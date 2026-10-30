# Spike record: Windows ARM64 (W0-05)

Date: 2026-10-29. Status: executed from this macOS arm64 workstation through public sources. No Windows machine or ARM64 build was used.

## Findings

| Question (ADR 0095) | Evidence | Answer |
|---|---|---|
| Is the WebView2 ARM64 static loader available? | The pinned SDK `Microsoft.Web.WebView2 1.0.2651.64` (the version `scripts/build_native_host_windows.ps1` pins, SHA-256 `c7909cdab68a56d1b8eeb1d5b18b0e00122ed812a075a8be785ed48de689eeca`) downloaded from `https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/1.0.2651.64/microsoft.web.webview2.1.0.2651.64.nupkg`. Its archive contains `build/native/arm64/WebView2LoaderStatic.lib`. The current release `1.0.4314.35` contains the same path. | Yes |
| Is a Windows ARM64 runner available to this repository? | The repository is public (`gh api repos/vindeckyy/OpenBoxGL`), so GitHub-hosted runners apply. The runner catalog (`https://raw.githubusercontent.com/actions/runner-images/main/README.md`) lists "Windows 11 Arm64" with the `windows-11-arm` label. | Label exists; first run not yet executed |
| Can the native host be built for ARM64 and smoke-tested? | Not executed. The build script has no `-Architecture` parameter yet, and no ARM64 device is available here. | Unknown until the CI job runs |
| Are ARM64 MSVC tools present? | Not checkable from macOS: `cl.exe` is Windows-only. Checked by the `windows-11-arm` job, not here. | Unknown until the CI job runs |

## Decision

Conditional GO, following ADR 0095:

1. Implement the ARM64 parameter in `scripts/build_native_host_windows.ps1` and the `windows-11-arm` CI job (W5-03, W5-04).
2. The ARM64 artifact ships in 2.0.0 only after a `windows-11-arm` job has built `OpenBox-arm64-windows.zip`, run the Windows suite, and launched a WebView2 window on ARM64.
3. If that job cannot pass before the release candidate, the ARM64 artifact moves to 2.0.1 and the installer keeps refusing ARM64 with the current message.

## Reproduce the check

```
curl -sS -o pin.nupkg "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/1.0.2651.64/microsoft.web.webview2.1.0.2651.64.nupkg"
shasum -a 256 pin.nupkg          # must equal the pinned hash
python3 -c "import zipfile; print('build/native/arm64/WebView2LoaderStatic.lib' in zipfile.ZipFile('pin.nupkg').namelist())"
```
