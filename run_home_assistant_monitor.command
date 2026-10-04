#!/bin/zsh -l

script_dir=${0:A:h}
cd "$script_dir" || exit 1
exec "$script_dir/.venv/bin/python" "$script_dir/multi_camera_monitor.py" --config "$script_dir/multi-camera.json"
