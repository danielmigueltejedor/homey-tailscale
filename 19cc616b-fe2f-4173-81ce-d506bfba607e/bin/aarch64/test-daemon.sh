#!/bin/sh
i=0
while true; do
  echo "external-binary:$i"
  i=$((i+1))
  sleep 2
done