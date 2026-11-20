//! Loopback TCP server lifecycle with `TCP_NODELAY`, port/token/flags publication,
//! and graceful shutdown. Mirrors `web_app.py::OpenBoxServer`.

use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::sync::Arc;

use openbox_runtime::AppState;
use openbox_store::secure_text_write;
use tokio::net::TcpListener;

use crate::router::{HttpState, build_router};
use crate::strangler::StranglerProxy;

pub struct RunningServer {
    pub port: u16,
    pub listener: TcpListener,
    pub http_state: Arc<HttpState>,
}

pub async fn bind_loopback(
    app: Arc<AppState>,
    requested_port: u16,
    strangler: Option<Arc<StranglerProxy>>,
) -> Result<RunningServer, String> {
    let addr = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), requested_port);
    let listener = TcpListener::bind(addr)
        .await
        .map_err(|e| format!("Failed to bind 127.0.0.1:{requested_port}: {e}"))?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("Failed reading bound port: {e}"))?
        .port();
    app.set_port(port);

    // Publish server.port, server.token, and native-host-flags with 0600 permissions
    let _ = secure_text_write(&app.data_dir.join("server.port"), &port.to_string());
    let _ = secure_text_write(&app.data_dir.join("server.token"), &app.token);
    let (tray, min_tray) = app
        .load_state_view()
        .ok()
        .map(|st| {
            let s = st.get("settings");
            let t = s
                .and_then(|o| o.get("tray_enabled"))
                .and_then(serde_json::Value::as_bool)
                .unwrap_or(false);
            let m = s
                .and_then(|o| o.get("minimize_to_tray"))
                .and_then(serde_json::Value::as_bool)
                .unwrap_or(false);
            (i32::from(t), i32::from(m))
        })
        .unwrap_or((0, 0));
    let _ = secure_text_write(
        &app.data_dir.join("native-host-flags"),
        &format!("{tray} {min_tray}\n"),
    );

    let http_state = HttpState::new(app, strangler);
    Ok(RunningServer {
        port,
        listener,
        http_state,
    })
}

pub async fn serve(server: RunningServer) -> Result<(), String> {
    let app = Arc::clone(&server.http_state.app);
    let strangler = server.http_state.strangler.clone();
    let router = build_router(server.http_state);
    let shutdown = Arc::clone(&app.shutdown_notify);
    let listener = server.listener;

    loop {
        tokio::select! {
            _ = shutdown.notified() => break,
            accepted = listener.accept() => {
                let (stream, peer_addr) = match accepted {
                    Ok(pair) => pair,
                    Err(err) => {
                        tracing::warn!("HTTP accept error: {err}");
                        continue;
                    }
                };
                let _ = stream.set_nodelay(true);
                let svc_router = router.clone();
                let conn_shutdown = Arc::clone(&shutdown);
                tokio::spawn(tokio::task::coop::unconstrained(async move {
                    let io = hyper_util::rt::TokioIo::new(stream);
                    let service = hyper::service::service_fn(
                        move |req: hyper::Request<hyper::body::Incoming>| {
                            let mut svc = svc_router.clone();
                            async move {
                                let mut axum_req = req.map(axum::body::Body::new);
                                axum_req
                                    .extensions_mut()
                                    .insert(axum::extract::ConnectInfo(peer_addr));
                                tower::ServiceExt::oneshot(&mut svc, axum_req).await
                            }
                        },
                    );
                    let conn = hyper::server::conn::http1::Builder::new()
                        .writev(true)
                        .serve_connection(io, service);
                    tokio::pin!(conn);
                    tokio::select! {
                        res = conn.as_mut() => {
                            if let Err(err) = res {
                                tracing::debug!("HTTP connection ended: {err}");
                            }
                        }
                        _ = conn_shutdown.notified() => {
                            conn.as_mut().graceful_shutdown();
                            let _ = conn.await;
                        }
                    }
                }));
            }
        }
    }

    if let Some(proxy) = strangler {
        proxy.shutdown();
    }
    Ok(())
}
