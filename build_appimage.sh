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

for file in openbox.py web_app.py importers.py arcade.py catalog.py cloud_sync.py emulators.py retroachievements.py plugins.py plugin_runner.py metadata.py archives.py saves.py updates.py env_config.py parity_discovery.py parity_import.py parity_integrations.py parity_media.py parity_saves.py parity_storefront.py plugin_catalog.py parity_premium.py stock_themes.py parity_gameyfin.py parity_save_tools.py parity_filter_presets.py parity_deeplinks.py parity_backup.py parity_tracking.py parity_igdb.py parity_emulator_defs.py parity_import_policy.py parity_gamescope.py index.html; do
  cp "$source_root/$file" "$appdir/usr/share/openbox/$file"
done
mkdir -p "$appdir/usr/share/openbox/emulator_defs"
cp "$source_root"/emulator_defs/*.yaml "$appdir/usr/share/openbox/emulator_defs/"
install -Dm755 "$source_root/scripts/openbox-launcher.sh" "$appdir/usr/share/openbox/openbox-launcher.sh"
mkdir -p "$appdir/usr/share/openbox/plugins"
cp "$source_root/plugins/catalog.json" "$appdir/usr/share/openbox/plugins/catalog.json"
mkdir -p "$appdir/usr/share/openbox/themes"
cp "$source_root"/themes/*.css "$appdir/usr/share/openbox/themes/"
sed 's/^Exec=.*/Exec=AppRun %u/' "$source_root/openbox.desktop" > "$appdir/usr/share/applications/openbox.desktop"
cp "$source_root/openbox.svg" "$appdir/usr/share/icons/hicolor/scalable/apps/openbox.svg"
cp "$source_root/openbox.metainfo.xml" "$appdir/usr/share/metainfo/openbox.appdata.xml"
cp "$source_root/LICENSE" "$appdir/usr/share/openbox/LICENSE"

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
app_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONHOME="$app_root/usr"
export PYTHONPATH="$app_root/usr/share/openbox"
export LD_LIBRARY_PATH="$app_root/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export TCL_LIBRARY="$app_root/usr/share/tcltk/tcl8.6"
export TK_LIBRARY="$app_root/usr/share/tcltk/tk8.6"
if [ "${1:-}" = "--native" ]; then
  shift
  exec "$app_root/usr/bin/python3" "$app_root/usr/share/openbox/openbox.py" "$@"
fi
exec "$app_root/usr/bin/python3" "$app_root/usr/share/openbox/web_app.py" "$@"
EOF
ln -s usr/share/applications/openbox.desktop "$appdir/openbox.desktop"
ln -s usr/share/icons/hicolor/scalable/apps/openbox.svg "$appdir/openbox.svg"
ln -s openbox.svg "$appdir/.DirIcon"

tool="$build_root/tools/appimagetool-x86_64.AppImage"
if [ ! -x "$tool" ]; then
  curl -L --fail --output "$tool" "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
  chmod +x "$tool"
fi
update_information="${OPENBOX_UPDATE_INFORMATION:-gh-releases-zsync|vindeckyy|OpenBoxGL|latest|OpenBox-x86_64.AppImage.zsync}"
arguments=(-n -u "$update_information" "$appdir" "$output")
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$tool" "${arguments[@]}"
chmod +x "$output"
