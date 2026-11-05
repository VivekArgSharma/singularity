#!/bin/sh
# Container entry point: a virtual display for OpenGL, then the engine (offscreen) with the console on $PORT.
set -e
Xvfb :99 -screen 0 1024x768x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
export DISPLAY=:99
for i in 1 2 3 4 5 6 7 8 9 10; do
    [ -e /tmp/.X11-unix/X99 ] && break
    sleep 0.3
done
exec python engine/sim.py --headless --admin-port "${PORT:-7860}"
