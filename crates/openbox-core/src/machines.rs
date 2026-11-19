//! Declared state machines mirroring `pkg/state/machines.py` (ADR 0107).

use std::collections::{BTreeMap, BTreeSet};
use std::sync::LazyLock;

pub const SESSION_ACTIVE: &str = "active";
pub const SESSION_STOPPING: &str = "stopping";
pub const SESSION_ABANDONED: &str = "abandoned";

pub const OPERATION_ACTIVE: &[&str] = &["queued", "running", "cancelling"];
pub const OPERATION_TERMINAL: &[&str] = &["done", "partial", "error", "cancelled", "interrupted"];

pub const PROGRESS_VALUES: &[&str] = &[
    "",
    "Unplayed",
    "Playing",
    "Paused",
    "Beaten",
    "Completed",
    "Mastered",
    "Abandoned",
];

pub type TransitionEdge = (Option<&'static str>, Option<&'static str>);
pub type TransitionMap = BTreeMap<&'static str, BTreeSet<TransitionEdge>>;

#[derive(Debug, Clone)]
pub struct Machine {
    pub name: &'static str,
    pub states: BTreeSet<&'static str>,
    pub terminal: BTreeSet<&'static str>,
    pub active: Option<BTreeSet<&'static str>>,
    pub derived: BTreeSet<&'static str>,
    pub transitions: TransitionMap,
    pub storage: &'static str,
    pub persisted: bool,
    pub exposed_via: &'static str,
}

impl Machine {
    pub fn allows(&self, action: &str, from: Option<&str>, to: Option<&str>) -> bool {
        let Some(edges) = self.transitions.get(action) else {
            return false;
        };
        edges.iter().any(|(s, t)| *s == from && *t == to)
    }

    pub fn targets(&self, action: &str, from: Option<&str>) -> BTreeSet<Option<&'static str>> {
        let Some(edges) = self.transitions.get(action) else {
            return BTreeSet::new();
        };
        edges
            .iter()
            .filter_map(|(s, t)| if *s == from { Some(*t) } else { None })
            .collect()
    }
}

pub fn facade_to_operation(state: &str) -> Option<&'static str> {
    match state {
        "installing" | "updating" | "downloading" | "running" => Some("running"),
        "done" => Some("done"),
        "partial" => Some("partial"),
        "error" => Some("error"),
        _ => None,
    }
}

pub fn job_result_state(state: Option<&str>) -> &str {
    match state {
        None | Some("") | Some("done") | Some("partial") | Some("error") => "ready",
        Some(other) => other,
    }
}

fn everything(states: &[&'static str]) -> BTreeSet<(Option<&'static str>, Option<&'static str>)> {
    let mut set = BTreeSet::new();
    for &s in states {
        for &t in states {
            set.insert((Some(s), Some(t)));
        }
    }
    set
}

pub static MACHINES: LazyLock<BTreeMap<&'static str, Machine>> = LazyLock::new(|| {
    let mut m = BTreeMap::new();

    // 1. operation
    {
        let states = [
            "queued",
            "running",
            "cancelling",
            "done",
            "partial",
            "error",
            "cancelled",
            "interrupted",
        ];
        let mut tr = BTreeMap::new();
        tr.insert("submit", BTreeSet::from([(None, Some("queued"))]));
        tr.insert(
            "start",
            BTreeSet::from([
                (Some("queued"), Some("running")),
                (Some("running"), Some("running")),
            ]),
        );
        tr.insert(
            "progress",
            OPERATION_ACTIVE
                .iter()
                .map(|&s| (Some(s), Some(s)))
                .collect(),
        );
        tr.insert(
            "cancel",
            OPERATION_ACTIVE
                .iter()
                .map(|&s| (Some(s), Some("cancelling")))
                .collect(),
        );
        let mut finish = BTreeSet::new();
        for &s in &states {
            for &t in OPERATION_TERMINAL {
                finish.insert((Some(s), Some(t)));
            }
        }
        tr.insert("finish", finish);
        tr.insert(
            "interrupt",
            OPERATION_ACTIVE
                .iter()
                .map(|&s| (Some(s), Some("interrupted")))
                .collect(),
        );
        tr.insert(
            "retry",
            OPERATION_TERMINAL
                .iter()
                .map(|&s| (Some(s), None))
                .collect(),
        );
        tr.insert("resume", BTreeSet::from([(Some("interrupted"), None)]));
        m.insert(
            "operation",
            Machine {
                name: "operation",
                states: states.into_iter().collect(),
                terminal: OPERATION_TERMINAL.iter().copied().collect(),
                active: Some(OPERATION_ACTIVE.iter().copied().collect()),
                derived: BTreeSet::new(),
                transitions: tr,
                storage: "operations.json operations[].state",
                persisted: true,
                exposed_via: "/api/v2/jobs, /api/jobs, job.* SSE",
            },
        );
    }

    // 2. legacy_job
    {
        let mut tr = BTreeMap::new();
        tr.insert("submit", BTreeSet::from([(None, Some("queued"))]));
        tr.insert("start", BTreeSet::from([(Some("queued"), Some("running"))]));
        tr.insert(
            "finish",
            BTreeSet::from([
                (Some("running"), Some("done")),
                (Some("running"), Some("error")),
            ]),
        );
        let cancel_edges = BTreeSet::from([
            (Some("queued"), Some("cancelled")),
            (Some("running"), Some("cancelled")),
        ]);
        tr.insert("cancel", cancel_edges.clone());
        tr.insert("supersede", cancel_edges.clone());
        tr.insert("interrupt", cancel_edges);
        m.insert(
            "legacy_job",
            Machine {
                name: "legacy_job",
                states: ["queued", "running", "done", "error", "cancelled"]
                    .into_iter()
                    .collect(),
                terminal: ["done", "error", "cancelled"].into_iter().collect(),
                active: Some(["queued", "running"].into_iter().collect()),
                derived: BTreeSet::new(),
                transitions: tr,
                storage: "job_manager.JobManager._jobs[name]['state']",
                persisted: false,
                exposed_via: "/api/v2/jobs",
            },
        );
    }

    // 3. job_facade
    {
        let states = [
            "idle",
            "installing",
            "updating",
            "downloading",
            "running",
            "done",
            "partial",
            "error",
        ];
        let mut tr = BTreeMap::new();
        tr.insert(
            "set",
            states
                .iter()
                .filter(|&&s| s != "idle")
                .map(|&s| (None, Some(s)))
                .collect(),
        );
        tr.insert("reset", states.iter().map(|&s| (Some(s), None)).collect());
        m.insert(
            "job_facade",
            Machine {
                name: "job_facade",
                states: states.into_iter().collect(),
                terminal: ["done", "partial", "error"].into_iter().collect(),
                active: Some(
                    ["installing", "updating", "downloading", "running"]
                        .into_iter()
                        .collect(),
                ),
                derived: BTreeSet::new(),
                transitions: tr,
                storage: "webapp_state facades; idle is a read-time default and never stored",
                persisted: false,
                exposed_via: "/api/emulators, /api/metadata/status, /api/media/*",
            },
        );
    }

    // 4. session
    {
        let states = [SESSION_ACTIVE, SESSION_STOPPING, SESSION_ABANDONED];
        let mut tr = BTreeMap::new();
        tr.insert("start", BTreeSet::from([(None, Some(SESSION_ACTIVE))]));
        tr.insert(
            "stop",
            BTreeSet::from([(Some(SESSION_ACTIVE), Some(SESSION_STOPPING))]),
        );
        tr.insert(
            "abandon",
            BTreeSet::from([
                (Some(SESSION_ACTIVE), Some(SESSION_ABANDONED)),
                (Some(SESSION_STOPPING), Some(SESSION_ABANDONED)),
            ]),
        );
        tr.insert("remove", states.iter().map(|&s| (Some(s), None)).collect());
        m.insert(
            "session",
            Machine {
                name: "session",
                states: states.into_iter().collect(),
                terminal: [SESSION_ABANDONED].into_iter().collect(),
                active: None,
                derived: BTreeSet::new(),
                transitions: tr,
                storage: "library.json active_sessions[].status (sessions.json crash mirror)",
                persisted: true,
                exposed_via: "/api/running, /api/sessions, session.* SSE",
            },
        );
    }

    // 5. setup_preview
    {
        let mut tr = BTreeMap::new();
        tr.insert("create", BTreeSet::from([(None, Some("ready"))]));
        tr.insert("queue", BTreeSet::from([(Some("ready"), Some("queued"))]));
        tr.insert("start", BTreeSet::from([(Some("queued"), Some("running"))]));
        tr.insert(
            "complete",
            BTreeSet::from([(Some("running"), Some("ready"))]),
        );
        tr.insert("fail", BTreeSet::from([(Some("running"), Some("error"))]));
        tr.insert(
            "interrupt",
            BTreeSet::from([
                (Some("queued"), Some("interrupted")),
                (Some("running"), Some("interrupted")),
            ]),
        );
        m.insert(
            "setup_preview",
            Machine {
                name: "setup_preview",
                states: [
                    "queued",
                    "running",
                    "cancelling",
                    "ready",
                    "error",
                    "interrupted",
                ]
                .into_iter()
                .collect(),
                terminal: ["ready", "error"].into_iter().collect(),
                active: None,
                derived: ["interrupted"].into_iter().collect(),
                transitions: tr,
                storage: "previews/<preview_id>.json state",
                persisted: true,
                exposed_via: "/api/v2/setup/preview",
            },
        );
    }

    // 6. match_preview
    {
        let mut tr = BTreeMap::new();
        tr.insert("create", BTreeSet::from([(None, Some("queued"))]));
        tr.insert("default", BTreeSet::from([(None, Some("ready"))]));
        tr.insert("start", BTreeSet::from([(Some("queued"), Some("running"))]));
        tr.insert(
            "complete",
            BTreeSet::from([(Some("running"), Some("ready"))]),
        );
        tr.insert("expire", BTreeSet::from([(Some("ready"), Some("expired"))]));
        tr.insert("fail", BTreeSet::from([(Some("running"), Some("error"))]));
        m.insert(
            "match_preview",
            Machine {
                name: "match_preview",
                states: [
                    "queued",
                    "running",
                    "cancelling",
                    "ready",
                    "expired",
                    "error",
                ]
                .into_iter()
                .collect(),
                terminal: ["ready", "expired", "error"].into_iter().collect(),
                active: None,
                derived: ["expired"].into_iter().collect(),
                transitions: tr,
                storage: "match_previews/<preview_id>.json state",
                persisted: true,
                exposed_via: "/api/v2/metadata/matches/preview",
            },
        );
    }

    // 7. storefront_source
    {
        let mut tr = BTreeMap::new();
        tr.insert("pending", BTreeSet::from([(None, Some("pending"))]));
        tr.insert(
            "start",
            BTreeSet::from([(Some("pending"), Some("running"))]),
        );
        tr.insert(
            "skip",
            BTreeSet::from([
                (Some("pending"), Some("skipped")),
                (Some("running"), Some("skipped")),
            ]),
        );
        tr.insert("finish", BTreeSet::from([(Some("running"), Some("ok"))]));
        tr.insert(
            "timeout",
            BTreeSet::from([(Some("running"), Some("timeout"))]),
        );
        tr.insert("fail", BTreeSet::from([(Some("running"), Some("error"))]));
        m.insert(
            "storefront_source",
            Machine {
                name: "storefront_source",
                states: ["pending", "running", "skipped", "ok", "timeout", "error"]
                    .into_iter()
                    .collect(),
                terminal: ["skipped", "ok", "timeout", "error"].into_iter().collect(),
                active: None,
                derived: BTreeSet::new(),
                transitions: tr,
                storage: "storefront-status.json sources[].status",
                persisted: true,
                exposed_via: "/api/v2/storefront/status",
            },
        );
    }

    // 8. media_queue_worker
    {
        let mut tr = BTreeMap::new();
        tr.insert(
            "start",
            BTreeSet::from([
                (None, Some("running")),
                (Some("stopped"), Some("running")),
                (Some("cancelled"), Some("running")),
                (Some("paused"), Some("running")),
            ]),
        );
        tr.insert("pause", BTreeSet::from([(Some("running"), Some("paused"))]));
        tr.insert(
            "resume",
            BTreeSet::from([(Some("paused"), Some("running"))]),
        );
        tr.insert(
            "cancel",
            BTreeSet::from([
                (Some("running"), Some("cancelled")),
                (Some("paused"), Some("cancelled")),
                (Some("stopped"), Some("cancelled")),
            ]),
        );
        tr.insert("stop", BTreeSet::from([(Some("running"), Some("stopped"))]));
        m.insert(
            "media_queue_worker",
            Machine {
                name: "media_queue_worker",
                states: ["stopped", "running", "paused", "cancelled"]
                    .into_iter()
                    .collect(),
                terminal: ["stopped", "cancelled"].into_iter().collect(),
                active: Some(["running", "paused"].into_iter().collect()),
                derived: BTreeSet::new(),
                transitions: tr,
                storage: "media queue worker state",
                persisted: false,
                exposed_via: "/api/media/queue",
            },
        );
    }

    // 9. media_queue_item
    {
        let mut tr = BTreeMap::new();
        tr.insert("queue", BTreeSet::from([(None, Some("queued"))]));
        tr.insert("fail", BTreeSet::from([(Some("queued"), Some("failed"))]));
        tr.insert("retry", BTreeSet::from([(Some("failed"), Some("queued"))]));
        m.insert(
            "media_queue_item",
            Machine {
                name: "media_queue_item",
                states: ["queued", "failed"].into_iter().collect(),
                terminal: ["failed"].into_iter().collect(),
                active: None,
                derived: BTreeSet::new(),
                transitions: tr,
                storage: "media-queue.json items[].state",
                persisted: true,
                exposed_via: "/api/media/queue",
            },
        );
    }

    // 10. game_progress
    {
        let mut tr = BTreeMap::new();
        tr.insert("set", everything(PROGRESS_VALUES));
        m.insert(
            "game_progress",
            Machine {
                name: "game_progress",
                states: PROGRESS_VALUES.iter().copied().collect(),
                terminal: BTreeSet::new(),
                active: None,
                derived: BTreeSet::new(),
                transitions: tr,
                storage: "library.json games[].progress",
                persisted: true,
                exposed_via: "/api/v2/library/progress/set, /api/game",
            },
        );
    }

    // 11. household_challenge
    {
        let states = ["open", "active", "closed", "cancelled", "completed"];
        let mut tr = BTreeMap::new();
        tr.insert("create", BTreeSet::from([(None, Some("open"))]));
        tr.insert("activate", BTreeSet::from([(Some("open"), Some("active"))]));
        tr.insert(
            "complete",
            BTreeSet::from([
                (Some("open"), Some("completed")),
                (Some("active"), Some("completed")),
            ]),
        );
        tr.insert(
            "close",
            BTreeSet::from([
                (Some("open"), Some("closed")),
                (Some("active"), Some("closed")),
            ]),
        );
        tr.insert(
            "cancel",
            BTreeSet::from([
                (Some("open"), Some("cancelled")),
                (Some("active"), Some("cancelled")),
            ]),
        );
        tr.insert("merge", everything(&states));
        m.insert(
            "household_challenge",
            Machine {
                name: "household_challenge",
                states: states.into_iter().collect(),
                terminal: ["closed", "cancelled", "completed"].into_iter().collect(),
                active: None,
                derived: ["complete", "expired"].into_iter().collect(),
                transitions: tr,
                storage: "library.json household.records[].payload.status",
                persisted: true,
                exposed_via: "/api/v2/household/*",
            },
        );
    }

    // 12. gameyfin_install
    {
        let mut tr = BTreeMap::new();
        tr.insert("start", BTreeSet::from([(None, Some("installing"))]));
        tr.insert(
            "complete",
            BTreeSet::from([(Some("installing"), Some("done"))]),
        );
        tr.insert(
            "fail",
            BTreeSet::from([(Some("installing"), Some("error"))]),
        );
        m.insert(
            "gameyfin_install",
            Machine {
                name: "gameyfin_install",
                states: ["idle", "installing", "done", "error"]
                    .into_iter()
                    .collect(),
                terminal: ["done", "error"].into_iter().collect(),
                active: Some(["installing"].into_iter().collect()),
                derived: BTreeSet::new(),
                transitions: tr,
                storage: "INSTALLS['gameyfin:<id>']['state']",
                persisted: false,
                exposed_via: "/api/gameyfin/install/status",
            },
        );
    }

    // 13-17: Declared-only frontend machines
    let wizard_steps = ["1", "2", "3", "4", "5", "6", "7", "8"];
    m.insert(
        "setup_wizard",
        Machine {
            name: "setup_wizard",
            states: wizard_steps.into_iter().collect(),
            terminal: ["8"].into_iter().collect(),
            active: None,
            derived: BTreeSet::new(),
            transitions: BTreeMap::from([("goto", everything(&wizard_steps))]),
            storage: "static/setup.js state.step + state.advanced (DOM)",
            persisted: false,
            exposed_via: "setup wizard UI",
        },
    );

    let party_states = ["setup", "spinning", "spun", "winner"];
    m.insert(
        "party_wheel",
        Machine {
            name: "party_wheel",
            states: party_states.into_iter().collect(),
            terminal: BTreeSet::new(),
            active: None,
            derived: BTreeSet::new(),
            transitions: BTreeMap::from([
                ("build", BTreeSet::from([(None, Some("setup"))])),
                (
                    "spin",
                    BTreeSet::from([
                        (Some("setup"), Some("spinning")),
                        (Some("spun"), Some("spinning")),
                    ]),
                ),
                ("settle", BTreeSet::from([(Some("spinning"), Some("spun"))])),
                ("win", BTreeSet::from([(Some("spun"), Some("winner"))])),
                ("next", BTreeSet::from([(Some("winner"), Some("spinning"))])),
                (
                    "reset",
                    party_states
                        .iter()
                        .map(|&s| (Some(s), Some("setup")))
                        .collect(),
                ),
            ]),
            storage: "static/party.js queue/spinning/spun (DOM)",
            persisted: false,
            exposed_via: "party overlay UI",
        },
    );

    let picker_states = ["idle", "spinning", "result", "empty"];
    m.insert(
        "picker_phase",
        Machine {
            name: "picker_phase",
            states: picker_states.into_iter().collect(),
            terminal: ["result", "empty"].into_iter().collect(),
            active: None,
            derived: BTreeSet::new(),
            transitions: BTreeMap::from([
                ("open", BTreeSet::from([(None, Some("idle"))])),
                ("pick", BTreeSet::from([(Some("idle"), Some("spinning"))])),
                (
                    "result",
                    BTreeSet::from([(Some("spinning"), Some("result"))]),
                ),
                ("empty", BTreeSet::from([(Some("spinning"), Some("empty"))])),
                (
                    "close",
                    picker_states
                        .iter()
                        .map(|&s| (Some(s), Some("idle")))
                        .collect(),
                ),
            ]),
            storage: "static/picker.js #pickerSpinning/#pickerResult hidden flags (DOM)",
            persisted: false,
            exposed_via: "picker dialog",
        },
    );

    let overlay_layers = [
        "bigbox",
        "menu",
        "pause",
        "screensaver",
        "party",
        "startup_video",
        "backdrop",
    ];
    m.insert(
        "overlay_stack",
        Machine {
            name: "overlay_stack",
            states: overlay_layers.into_iter().collect(),
            terminal: BTreeSet::new(),
            active: None,
            derived: BTreeSet::new(),
            transitions: BTreeMap::from([
                (
                    "show",
                    overlay_layers.iter().map(|&l| (None, Some(l))).collect(),
                ),
                (
                    "hide",
                    overlay_layers.iter().map(|&l| (Some(l), None)).collect(),
                ),
            ]),
            storage: "static/bigbox.js per-layer hidden flags (DOM); attract = all hidden",
            persisted: false,
            exposed_via: "Big Box UI",
        },
    );

    let repair_states = ["scan", "preview", "applied"];
    m.insert(
        "repair_wizard",
        Machine {
            name: "repair_wizard",
            states: repair_states.into_iter().collect(),
            terminal: ["applied"].into_iter().collect(),
            active: None,
            derived: BTreeSet::new(),
            transitions: BTreeMap::from([
                ("scan", BTreeSet::from([(None, Some("scan"))])),
                ("preview", BTreeSet::from([(Some("scan"), Some("preview"))])),
                (
                    "apply",
                    BTreeSet::from([(Some("preview"), Some("applied"))]),
                ),
                (
                    "reset",
                    repair_states
                        .iter()
                        .map(|&s| (Some(s), Some("scan")))
                        .collect(),
                ),
            ]),
            storage: "static/library.js repair* variables (DOM)",
            persisted: false,
            exposed_via: "library repair wizard",
        },
    );

    m
});
