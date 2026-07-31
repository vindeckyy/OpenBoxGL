#!/bin/bash
set -euo pipefail

source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
output="${1:-$source_root/OpenBox-x86_64.AppImage}"
build_root="$source_root/build"
mkdir -p "$build_root/tools"
temporary="$(mktemp -d "$build_root/appimage.XXXXXX")"
trap 'rm -rf -- "$temporary"' EXIT
appdir="$temporary/OpenBox.AppDir"
mkdir -p "$appdir/usr/bin" "$appdir/usr/lib" "$appdir/usr/share/openbox" "$appdir/usr/share/applications" "$appdir/usr/share/icons/hicolor/scalable/apps" "$appdir/usr/share/metainfo"

python_binary="$(readlink -f "$(command -v python3)")"
stdlib="$(python3 -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')"
python_version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
cp "$python_binary" "$appdir/usr/bin/python3"
cp -a "$stdlib" "$appdir/usr/lib/python$python_version"
find "$appdir/usr/lib/python$python_version" -type d -name __pycache__ -prune -exec rm -rf -- {} +

while IFS= read -r file; do
  [ -n "$file" ] || continue
  cp "$source_root/$file" "$appdir/usr/share/openbox/$file"
done < "$source_root/runtime_modules.txt"
cp "$source_root/index.html" "$appdir/usr/share/openbox/index.html"
mkdir -p "$appdir/usr/share/openbox/emulator_defs"
cp "$source_root"/emulator_defs/*.yaml "$appdir/usr/share/openbox/emulator_defs/"
install -Dm755 "$source_root/scripts/openbox-launcher.sh" "$appdir/usr/share/openbox/openbox-launcher.sh"
mkdir -p "$appdir/usr/share/openbox/plugins"
cp "$source_root/plugins/catalog.json" "$appdir/usr/share/openbox/plugins/catalog.json"
mkdir -p "$appdir/usr/share/openbox/themes"
cp "$source_root"/themes/*.css "$appdir/usr/share/openbox/themes/"

while IFS= read -r library; do
  cp -L "$library" "$appdir/usr/lib/$(basename "$library")"
done < <(
  find "$appdir/usr/bin/python3" "$appdir/usr/lib/python$python_version/lib-dynload" -type f -print0 |
    xargs -0 -n1 ldd 2>/dev/null |
    awk '/=> \// {print $3} /^\// {print $1}' |
    grep -vE '/(libc|libm|libpthread|libdl|librt|ld-linux)[^/]*\.so' |
    sort -u
)

for data in /usr/share/tcltk/tcl8.6 /usr/share/tcltk/tk8.6; do
  if [ -d "$data" ]; then
    mkdir -p "$appdir/usr/share/tcltk"
    cp -a "$data" "$appdir/usr/share/tcltk/"
  fi
done

install -m 755 /dev/stdin "$appdir/AppRun" <<'EOF'
#!/bin/bash
# Keep bundled libs on the Python process only. A sticky LD_LIBRARY_PATH leaks into
# xdg-open/host browsers and breaks Gear Lever / desktop-menu launches.
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
export TCL_LIBRARY="$app_root/usr/share/tcltk/tcl8.6"
export TK_LIBRARY="$app_root/usr/share/tcltk/tk8.6"
lib_path="$app_root/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
python="$app_root/usr/bin/python3"
if [ "${1:-}" = "--native" ]; then
  shift
  exec env LD_LIBRARY_PATH="$lib_path" "$python" "$app_root/usr/share/openbox/openbox.py" "$@"
fi
exec env LD_LIBRARY_PATH="$lib_path" "$python" "$app_root/usr/share/openbox/web_app.py" "$@"
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

tool="$build_root/tools/appimagetool-x86_64.AppImage"
if [ ! -x "$tool" ]; then
  curl -L --fail --output "$tool" "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
  chmod +x "$tool"
fi
update_information="${OPENBOX_UPDATE_INFORMATION:-gh-releases-zsync|vindeckyy|OpenBoxGL|latest|OpenBox-x86_64.AppImage.zsync}"
arguments=(-n -u "$update_information" "$appdir" "$output")
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$tool" "${arguments[@]}"
chmod +x "$output"
