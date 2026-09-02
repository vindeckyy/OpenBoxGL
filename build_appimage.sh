#!/bin/bash
set -euo pipefail

source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Bundle the host interpreter and libraries, so the artifact arch follows the
# build host. OPENBOX_ARCH overrides detection (CI cross-builds set it).
arch="${OPENBOX_ARCH:-$(uname -m)}"
case "$arch" in
  x86_64|amd64) arch="x86_64" ;;
  aarch64|arm64) arch="aarch64" ;;
  *) echo "build_appimage.sh: unsupported architecture: $arch" >&2; exit 1 ;;
esac
output="${1:-$source_root/OpenBox-$arch.AppImage}"
build_root="$source_root/build"
mkdir -p "$build_root/tools"
temporary="$(mktemp -d "$build_root/appimage.XXXXXX")"
trap 'rm -rf -- "$temporary"' EXIT
appdir="$temporary/OpenBox.AppDir"
mkdir -p "$appdir/usr/bin" "$appdir/usr/lib" "$appdir/usr/share/openbox" "$appdir/usr/share/openbox/handlers" "$appdir/usr/share/applications" "$appdir/usr/share/icons/hicolor/scalable/apps" "$appdir/usr/share/metainfo"

python_binary="$(readlink -f "$(command -v python3)")"
stdlib="$(python3 -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')"
python_version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
cp "$python_binary" "$appdir/usr/bin/python3"
cp -a "$stdlib" "$appdir/usr/lib/python$python_version"
find "$appdir/usr/lib/python$python_version" -type d -name __pycache__ -prune -exec rm -rf -- {} +

while IFS= read -r file; do
  file="$(echo "$file" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  [ -n "$file" ] || continue
  [[ "$file" =~ ^# ]] && continue
  if [ ! -f "$source_root/$file" ]; then
    echo "build_appimage.sh: missing runtime module: $file" >&2
    exit 1
  fi
  install -Dm644 "$source_root/$file" "$appdir/usr/share/openbox/$file"
done < "$source_root/runtime_modules.txt"
cp "$source_root/index.html" "$appdir/usr/share/openbox/index.html"
cp "$source_root/openbox.svg" "$appdir/usr/share/openbox/openbox.svg"
mkdir -p "$appdir/usr/share/openbox/static"
cp "$source_root"/static/*.js "$source_root"/static/*.css "$appdir/usr/share/openbox/static/"
mkdir -p "$appdir/usr/share/openbox/locales"
cp "$source_root"/locales/*.json "$appdir/usr/share/openbox/locales/"
mkdir -p "$appdir/usr/share/openbox/assets"
cp "$source_root"/assets/openbox-logo.png "$appdir/usr/share/openbox/assets/"
mkdir -p "$appdir/usr/share/openbox/emulator_defs"
cp "$source_root"/emulator_defs/*.yaml "$appdir/usr/share/openbox/emulator_defs/"
install -Dm755 "$source_root/scripts/openbox-launcher.sh" "$appdir/usr/share/openbox/openbox-launcher.sh"
install -Dm755 "$source_root/openbox-native.sh" "$appdir/usr/share/openbox/openbox-native.sh"
mkdir -p "$appdir/usr/share/openbox/plugins"
cp "$source_root/plugins/catalog.json" "$appdir/usr/share/openbox/plugins/catalog.json"
mkdir -p "$appdir/usr/share/openbox/themes"
cp "$source_root"/themes/*.css "$appdir/usr/share/openbox/themes/"
gcc -O2 "$source_root/native_host.c" -o "$appdir/usr/share/openbox/native_host" $(pkg-config --cflags --libs webkit2gtk-4.1)
while IFS= read -r library; do
  cp -L "$library" "$appdir/usr/lib/$(basename "$library")"
done < <(
  find "$appdir/usr/bin/python3" "$appdir/usr/lib/python$python_version/lib-dynload" -type f -print0 |
    xargs -0 -n1 ldd 2>/dev/null |
    awk '/=> \// {print $3} /^\// {print $1}' |
    grep -vE '/(libc|libm|libpthread|libdl|librt|ld-linux)[^/]*\.so' |
    sort -u
)

install -m 755 /dev/stdin "$appdir/AppRun" <<'EOF'
#!/bin/bash
# Keep bundled libraries scoped to the process that needs them. Exporting the
# path before entering a shell launcher makes /bin/bash resolve bundled
# readline/ncurses libraries and can abort desktop launches.
set -euo pipefail
if command -v readlink >/dev/null 2>&1; then
  app_root="$(cd -- "$(dirname -- "$(readlink -f -- "$0" 2>/dev/null || echo "$0")")" && pwd)"
else
  app_root="$(cd -- "$(dirname -- "$0")" && pwd)"
fi
export APPDIR="$app_root"
export PATH="$app_root/usr/bin:${PATH:-/usr/bin:/bin}"
export PYTHONHOME="$app_root/usr"
export PYTHONPATH="$app_root/usr/share/openbox${PYTHONPATH:+:$PYTHONPATH}"
lib_path="$app_root/usr/lib"
python="$app_root/usr/bin/python3"
data_dir="${OPENBOX_DATA_DIR:-$HOME/.local/share/openbox-game-launcher}"
mkdir -p "$data_dir"
# Capture loader-level failures (missing libwebkit2gtk, etc.) that are
# otherwise invisible on a file-manager double-click.
exec 2>>"$data_dir/openbox-launch.log"
if [ "${1:-}" = "--web" ]; then
  shift
  exec env LD_LIBRARY_PATH="$lib_path" "$python" "$app_root/usr/share/openbox/web_app.py" "$@"
fi
export OPENBOX_WEB_APP="$app_root/usr/share/openbox/web_app.py"
export OPENBOX_PYTHON="$python"
export APPDIR
export OPENBOX_BUNDLED_LIB_PATH="$lib_path"
unset LD_LIBRARY_PATH
exec "$app_root/usr/share/openbox/openbox-native.sh" "$@"
EOF

# Unique desktop/icon names avoid colliding with the Openbox window manager.
python3 - "$source_root/openbox.desktop" "$source_root/updates.py" > "$appdir/usr/share/applications/io.openbox.GameLauncher.desktop" <<'PY'
import pathlib, re, sys
desktop = pathlib.Path(sys.argv[1]).read_text()
version = "0.0.0"
for line in pathlib.Path(sys.argv[2]).read_text().splitlines():
    if line.startswith("VERSION = "):
        version = line.split("=", 1)[1].strip().strip('"').strip("'")
        break
desktop = re.sub(r"^Exec=.*$", "Exec=AppRun %u", desktop, count=1, flags=re.M)
desktop = re.sub(r"^Icon=.*$", "Icon=io.openbox.GameLauncher", desktop, count=1, flags=re.M)
if "StartupWMClass=" not in desktop:
    desktop = desktop.rstrip() + "\nStartupWMClass=OpenBox\n"
if "X-AppImage-Version=" not in desktop:
    desktop = desktop.rstrip() + f"\nX-AppImage-Version={version}\n"
sys.stdout.write(desktop if desktop.endswith("\n") else desktop + "\n")
PY
cp "$source_root/openbox.svg" "$appdir/usr/share/icons/hicolor/scalable/apps/io.openbox.GameLauncher.svg"
cp "$source_root/openbox.metainfo.xml" "$appdir/usr/share/metainfo/openbox.appdata.xml"
cp "$source_root/LICENSE" "$appdir/usr/share/openbox/LICENSE"
ln -s usr/share/applications/io.openbox.GameLauncher.desktop "$appdir/io.openbox.GameLauncher.desktop"
ln -s usr/share/icons/hicolor/scalable/apps/io.openbox.GameLauncher.svg "$appdir/io.openbox.GameLauncher.svg"
ln -s io.openbox.GameLauncher.svg "$appdir/.DirIcon"

# Record manifest of bundled files and generate CycloneDX SBOM
python3 - "$build_root/sbom-manifest.json" "$appdir" <<'PY'
import hashlib, json, pathlib, sys
out_path = pathlib.Path(sys.argv[1])
appdir = pathlib.Path(sys.argv[2])
records = []
for p in sorted(appdir.rglob("*")):
    if p.is_file() and not p.is_symlink():
        rel = p.relative_to(appdir).as_posix()
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        records.append({
            "path": rel,
            "sha256": digest,
            "size": p.stat().st_size
        })
manifest = {
    "appdir": str(appdir.name),
    "file_count": len(records),
    "files": sorted(records, key=lambda x: x["path"])
}
out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
python3 "$source_root/scripts/gen_sbom.py" --appdir "$appdir" --out "$build_root/sbom.json"
if [ -n "${OPENBOX_APPDIR:-}" ]; then
  preserved_appdir="$OPENBOX_APPDIR"
  mkdir -p "$preserved_appdir"
  cp -a "$appdir"/. "$preserved_appdir"/
fi

tool="$build_root/tools/appimagetool-$arch.AppImage"
case "$arch" in
  x86_64)
    tool_url="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
    tool_sha256="a6d71e2b6cd66f8e8d16c37ad164658985e0cf5fcaa950c90a482890cb9d13e0"
    ;;
  aarch64)
    tool_url="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-aarch64.AppImage"
    # Pinned 2026-09-02 from the continuous release; recompute when bumping.
    tool_sha256="1b00524ba8c6b678dc15ef88a5c25ec24def36cdfc7e3abb32ddcd068e8007fe"
    ;;
esac
if [ ! -x "$tool" ]; then
  downloaded_tool="$temporary/appimagetool-$arch.AppImage"
  curl --proto '=https' --tlsv1.2 --location --fail --silent --show-error --output "$downloaded_tool" "$tool_url"
  downloaded_hash="$(sha256sum "$downloaded_tool" | awk '{print $1}')"
  [ "$downloaded_hash" = "$tool_sha256" ] || {
    echo "build_appimage.sh: appimagetool checksum mismatch" >&2
    exit 1
  }
  mv "$downloaded_tool" "$tool"
  chmod +x "$tool"
fi
tool_hash="$(sha256sum "$tool" | awk '{print $1}')"
[ "$tool_hash" = "$tool_sha256" ] || {
  echo "build_appimage.sh: cached appimagetool checksum mismatch" >&2
  exit 1
}
update_information="${OPENBOX_UPDATE_INFORMATION:-gh-releases-zsync|vindeckyy|OpenBoxGL|latest|OpenBox-$arch.AppImage.zsync}"
arguments=(-n -u "$update_information" "$appdir" "$output")
ARCH=$arch APPIMAGE_EXTRACT_AND_RUN=1 "$tool" "${arguments[@]}"
chmod +x "$output"
