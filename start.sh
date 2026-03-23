#!/bin/bash
pkill -f 'remote-debugging-port' 2>/dev/null || true
sleep 1
cd /home/grom/ozon_spider
python3 run.py 2>&1
