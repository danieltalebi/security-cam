#!/bin/zsh

set -euo pipefail

project_dir=${0:A:h}
user_id=$(id -u)
daemon_label="com.danieltalebi.security-cam"
menu_label="com.danieltalebi.security-cam-dashboard"
legacy_menu_label="com.danieltalebi.security-cam-menubar"
launch_agents_dir="$HOME/Library/LaunchAgents"
logs_dir="$HOME/Library/Logs/security-cam"
app_dir="${SECURITY_CAM_DASHBOARD_APP:-$HOME/Applications/Security Cam.app}"
build_dashboard=true
if [[ -n "${SECURITY_CAM_DASHBOARD_APP:-}" ]]; then
  build_dashboard=false
fi
daemon_plist="$launch_agents_dir/$daemon_label.plist"
menu_plist="$launch_agents_dir/$menu_label.plist"
legacy_menu_plist="$launch_agents_dir/$legacy_menu_label.plist"
build_dir="$project_dir/.build/macos-app-service"

mkdir -p "$launch_agents_dir" "$logs_dir" "$HOME/Applications"
mkdir -p "$build_dir"
export CLANG_MODULE_CACHE_PATH="$build_dir/module-cache"
export SWIFT_MODULECACHE_PATH="$build_dir/module-cache"
mkdir -p "$CLANG_MODULE_CACHE_PATH"

if $build_dashboard; then
  mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources"
  echo "Building the native dashboard app…"
  /usr/bin/swiftc \
    -parse-as-library \
    -framework AppKit \
    "$project_dir/macos/SecurityCamMenu.swift" \
    -o "$build_dir/SecurityCam"
  cp "$build_dir/SecurityCam" "$app_dir/Contents/MacOS/SecurityCam"
  cp "$project_dir/macos/SecurityCamMenu-Info.plist" "$app_dir/Contents/Info.plist"
  if [[ -f "$project_dir/.build/macos-app/Security Cam.app/Contents/Resources/AppIcon.icns" ]]; then
    cp "$project_dir/.build/macos-app/Security Cam.app/Contents/Resources/AppIcon.icns" "$app_dir/Contents/Resources/AppIcon.icns"
  fi
  chmod 755 "$app_dir/Contents/MacOS/SecurityCam"
elif [[ ! -x "$app_dir/Contents/MacOS/SecurityCam" ]]; then
  echo "Dashboard app not found at $app_dir"
  exit 1
fi

make_daemon_plist() {
  local target=$1
  /usr/bin/plutil -create xml1 "$target"
  /usr/libexec/PlistBuddy -c "Add :Label string $daemon_label" "$target"
  /usr/libexec/PlistBuddy -c "Add :ProgramArguments array" "$target"
  /usr/libexec/PlistBuddy -c "Add :ProgramArguments:0 string /bin/zsh" "$target"
  /usr/libexec/PlistBuddy -c "Add :ProgramArguments:1 string -l" "$target"
  /usr/libexec/PlistBuddy -c "Add :ProgramArguments:2 string $project_dir/run_home_assistant_monitor.command" "$target"
  /usr/libexec/PlistBuddy -c "Add :WorkingDirectory string $project_dir" "$target"
  /usr/libexec/PlistBuddy -c "Add :RunAtLoad bool true" "$target"
  /usr/libexec/PlistBuddy -c "Add :KeepAlive dict" "$target"
  /usr/libexec/PlistBuddy -c "Add :KeepAlive:SuccessfulExit bool false" "$target"
  /usr/libexec/PlistBuddy -c "Add :ThrottleInterval integer 10" "$target"
  /usr/libexec/PlistBuddy -c "Add :ProcessType string Background" "$target"
  /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables dict" "$target"
  /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:PYTHONUNBUFFERED string 1" "$target"
  /usr/libexec/PlistBuddy -c "Add :StandardOutPath string $logs_dir/monitor.log" "$target"
  /usr/libexec/PlistBuddy -c "Add :StandardErrorPath string $logs_dir/monitor-error.log" "$target"
}

make_menu_plist() {
  local target=$1
  /usr/bin/plutil -create xml1 "$target"
  /usr/libexec/PlistBuddy -c "Add :Label string $menu_label" "$target"
  /usr/libexec/PlistBuddy -c "Add :ProgramArguments array" "$target"
  /usr/libexec/PlistBuddy -c "Add :ProgramArguments:0 string $app_dir/Contents/MacOS/SecurityCam" "$target"
  /usr/libexec/PlistBuddy -c "Add :ProgramArguments:1 string --background" "$target"
  /usr/libexec/PlistBuddy -c "Add :RunAtLoad bool true" "$target"
  /usr/libexec/PlistBuddy -c "Add :ProcessType string Interactive" "$target"
  /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables dict" "$target"
  /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:NSUnbufferedIO string YES" "$target"
  /usr/libexec/PlistBuddy -c "Add :StandardOutPath string $logs_dir/menu.log" "$target"
  /usr/libexec/PlistBuddy -c "Add :StandardErrorPath string $logs_dir/menu-error.log" "$target"
}

daemon_tmp=$(mktemp "$TMPDIR/security-cam-daemon.plist.XXXXXX")
menu_tmp=$(mktemp "$TMPDIR/security-cam-menu.plist.XXXXXX")
trap 'rm -f "$daemon_tmp" "$menu_tmp"' EXIT
make_daemon_plist "$daemon_tmp"
make_menu_plist "$menu_tmp"
/usr/bin/plutil -lint "$daemon_tmp" "$menu_tmp"

echo "Stopping the previous monitor, if present…"
for pid in $(pgrep -f "$project_dir/multi_camera_monitor.py --config $project_dir/multi-camera.json" 2>/dev/null || true); do
  kill -TERM "$pid" 2>/dev/null || true
done

/bin/launchctl bootout "gui/$user_id/$daemon_label" 2>/dev/null || true
/bin/launchctl bootout "gui/$user_id/$menu_label" 2>/dev/null || true
/bin/launchctl bootout "gui/$user_id/$legacy_menu_label" 2>/dev/null || true
if [[ -f "$legacy_menu_plist" ]]; then
  mv "$legacy_menu_plist" "$legacy_menu_plist.disabled"
fi
cp "$daemon_tmp" "$daemon_plist"
cp "$menu_tmp" "$menu_plist"
chmod 600 "$daemon_plist" "$menu_plist"

echo "Starting the daemon and menu bar app…"
if ! /bin/launchctl bootstrap "gui/$user_id" "$daemon_plist"; then
  sleep 1
  /bin/launchctl bootstrap "gui/$user_id" "$daemon_plist"
fi
if ! /bin/launchctl bootstrap "gui/$user_id" "$menu_plist"; then
  sleep 1
  /bin/launchctl bootstrap "gui/$user_id" "$menu_plist"
fi

sleep 2
if /bin/launchctl print "gui/$user_id/$daemon_label" 2>/dev/null | grep -q "state = running"; then
  echo "Security camera daemon is running."
else
  echo "The service was installed but is not running. Check $logs_dir/monitor-error.log"
  exit 1
fi

echo "Security Cam dashboard installed with menu bar controls."
