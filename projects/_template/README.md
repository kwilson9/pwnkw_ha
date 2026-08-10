# <Project Name>

> Part of **[pwnkw_ha](../../)** — Kenneth's collection of Home Assistant projects.

One or two sentences: what this project does and why it's cool.

| Control | What it does |
|---|---|
| <e.g. Tap tile> | <action> |

## How it works

Short explanation of the data flow / mechanism. A tiny diagram helps:

```
<source>  ──►  <transform>  ──►  <result>  ──►  <output>
```

## Requirements

- Home Assistant (built against 2026.8).
- <any integration, HACS card, or hardware this needs>

## Install

1. **Secret(s):** add any needed values to your `secrets.yaml`, and add matching placeholders to
   the repo's root [`secrets.yaml.example`](../../secrets.yaml.example) under this project's heading.
2. **Includes:** add the lines from [`configuration.example.yaml`](configuration.example.yaml) to
   your `configuration.yaml` (delete the file if the project needs none).
3. **Copy files** into your HA config — `sensors/`, `rest/`, merge `scripts.yaml`, etc.
4. **Restart / reload** as required (platform sensors need a restart).
5. **Dashboard:** add the cards from `dashboard/`.

## Files

```
sensors/                    # platform-style sensors (delete if unused)
rest/                       # REST integration sensors (delete if unused)
scripts.yaml                # scripts (delete if unused)
dashboard/                  # Lovelace cards (delete if unused)
configuration.example.yaml  # include lines to add (delete if none)
```

## Credits & Attribution

- <data source / library / API> — <link>

---

<!--
  New project checklist:
  1. Copy this _template folder to projects/<your-project>/
  2. Delete the .gitkeep files and any folders you don't use
  3. Fill in this README
  4. Add a row to the root README.md projects table
  5. Add any secrets to the root secrets.yaml.example
-->
