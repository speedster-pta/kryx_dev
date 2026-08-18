#!/usr/bin/env bash
# Same as promote_to_prod.sh, but makes production exactly match kryx-dev (--delete).
rsync -auv --exclude-from="$HOME/kryx-dev/.rsync-exclude" "$HOME/kryx-dev/" "$HOME/kryx/" --delete
