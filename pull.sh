#!/usr/bin/env bash
# Pull local project code from production VPS, excluding dev/simulation files
rsync -auv --exclude-from="/Users/phillip/Downloads/kryx/.rsync-exclude" kryx-cloud:~/kryx/ /Users/phillip/Downloads/kryx/
