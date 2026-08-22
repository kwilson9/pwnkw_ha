# Projects

Each folder here is a self-contained project with its own README and install steps.
The line under each name is what it needs and where its files go — check that first.

### [Discogs → Sonos Jukebox](discogs-jukebox/)

<p class="manifest">Home Assistant · sensors · rest · scripts.yaml · dashboard</p>
<p class="source"><a href="https://github.com/kwilson9/pwnkw_ha/tree/main/projects/discogs-jukebox">Source on GitHub</a></p>

A random record from your Discogs collection on a dashboard tile. Tap to play the album
on Sonos via Apple Music, double-tap to shuffle, hold to open Discogs. Includes a
"Play Artist Mix" button.

### [Mailbox Item Tracker](mailbox-tracker/)

<p class="manifest">Home Assistant · packages · dashboard</p>
<p class="source"><a href="https://github.com/kwilson9/pwnkw_ha/tree/main/projects/mailbox-tracker">Source on GitHub</a></p>

A live count of uncollected items in a remote or CMRA mailbox. Reads the provider's
"new mail" emails over IMAP and resets to zero when you arrive to collect.

### [Nanoleaf Room Clock](nanoleaf-clock-bar/)

<p class="manifest">AppDaemon · python · tools</p>
<p class="source"><a href="https://github.com/kwilson9/pwnkw_ha/tree/main/projects/nanoleaf-clock-bar">Source on GitHub</a></p>

A Nanoleaf Lines bar as a linear clock — minutes fill left to right, with an
outdoor-temperature gauge, an overnight heartbeat, and a meeting countdown that drains
the bar to empty exactly at meeting time. Drives the device over its local REST API.

## Starting a new one

Copy [`_template/`](https://github.com/kwilson9/pwnkw_ha/tree/main/projects/_template)
and follow *Adding a new project* in the [root README](../).

> `_template/` is browsable on GitHub but not on this site — Jekyll reserves
> leading-underscore directories and excludes them from the published build.
