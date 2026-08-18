#!/usr/bin/env bash
# Deploy local project code to kryx-dev, making the remote dir exactly match local
rsync -auv --exclude-from="/Users/phillip/Downloads/kryx/.rsync-exclude" /Users/phillip/Downloads/kryx/ kryx-cloud:~/kryx-dev/ --delete
