#!/usr/bin/env bash
# Deploy local project code to production VPS, excluding dev/simulation files
rsync -auv --exclude-from="/Users/phillip/Downloads/kryx/.rsync-exclude" /Users/phillip/Downloads/kryx/ kryx-cloud:~/kryx/
