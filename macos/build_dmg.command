#!/bin/zsh

set -euo pipefail

project_dir=${0:A:h:h}
build_dir="$project_dir/.build/macos-app"
dist_dir="$project_dir/dist"
app_name="Security Cam.app"
app_dir="$build_dir/$app_name"
icon_source="$project_dir/macos/Assets/security-camera-icon.png"
asset_catalog="$build_dir/SecurityCamAssets.xcassets"
iconset="$asset_catalog/AppIcon.appiconset"
staging="$build_dir/dmg"
version=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$project_dir/macos/SecurityCamMenu-Info.plist")
dmg_path="$dist_dir/SecurityCam-$version.dmg"

mkdir -p "$build_dir/module-cache" "$dist_dir"
rm -rf "$app_dir" "$asset_catalog" "$staging"
mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources" "$iconset" "$staging"

export CLANG_MODULE_CACHE_PATH="$build_dir/module-cache"
export SWIFT_MODULECACHE_PATH="$build_dir/module-cache"

echo "Building Security Cam…"
/usr/bin/swiftc \
  -parse-as-library \
  -framework AppKit \
  "$project_dir/macos/SecurityCamMenu.swift" \
  -o "$app_dir/Contents/MacOS/SecurityCam"

cp "$project_dir/macos/SecurityCamMenu-Info.plist" "$app_dir/Contents/Info.plist"
chmod 755 "$app_dir/Contents/MacOS/SecurityCam"

echo "Creating the macOS icon…"
for size in 16 32 128 256 512; do
  /usr/bin/sips -z "$size" "$size" "$icon_source" --out "$iconset/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  /usr/bin/sips -z "$double" "$double" "$icon_source" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
cp "$project_dir/macos/Assets/AppIconContents.json" "$iconset/Contents.json"
cp "$project_dir/macos/Assets/AssetCatalogContents.json" "$asset_catalog/Contents.json"
/usr/bin/xcrun actool \
  --compile "$app_dir/Contents/Resources" \
  --platform macosx \
  --minimum-deployment-target 13.0 \
  --app-icon AppIcon \
  --output-partial-info-plist "$build_dir/asset-info.plist" \
  "$asset_catalog" >/dev/null

echo "Signing the local build…"
/usr/bin/codesign --force --deep --sign - "$app_dir"

cp -R "$app_dir" "$staging/$app_name"
ln -s /Applications "$staging/Applications"

echo "Creating $dmg_path…"
rm -f "$dmg_path"
/usr/bin/hdiutil create \
  -volname "Security Cam" \
  -srcfolder "$staging" \
  -ov \
  -format UDZO \
  "$dmg_path"

echo "Built: $dmg_path"
