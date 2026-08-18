#!/usr/bin/env bash
# Deploy local project code to the kryx-dev environment on the Kryx VPS
rsync -auv --exclude-from="/Users/phillip/Downloads/kryx/.rsync-exclude" /Users/phillip/Downloads/kryx/ kryx-cloud:~/kryx-dev/
