#!/bin/bash
# Kill any existing chrome with remote debugging
pkill -f 'remote-debugging-port' 2>/dev/null || true
sleep 2

USER_DATA=$(mktemp -d /tmp/ozon_chrome_XXXXXX)
echo "user-data-dir: $USER_DATA"

export DISPLAY=:0

google-chrome-stable \
  --remote-debugging-port=9223 \
  --user-data-dir="$USER_DATA" \
  --no-first-run \
  --no-default-browser-check \
  --disable-default-apps \
  --disable-extensions \
  --disable-sync \
  --lang=ru-RU \
  --window-size=1920,1080 \
  about:blank > /tmp/chrome_test.log 2>&1 &

CHROME_PID=$!
echo "Chrome PID: $CHROME_PID"

for i in $(seq 1 20); do
  sleep 1
  RESULT=$(curl -s --max-time 2 http://127.0.0.1:9223/json/version 2>&1)
  if echo "$RESULT" | grep -q 'Browser'; then
    echo "CDP ready after ${i}s:"
    echo "$RESULT"
    exit 0
  fi
  echo "Waiting... $i"
done

echo "CDP not ready. Last chrome log:"
tail -5 /tmp/chrome_test.log
exit 1
