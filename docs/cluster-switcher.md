# Cluster Switcher

If you run more than one Cabotage, list them all so people can see which one
they are on and jump between them with <kbd>⌘J</kbd> (<kbd>Ctrl J</kbd> on
Windows and Linux).

## Set up

Set these on every Cabotage:

| Variable | What it does |
|----------|--------------|
| `CABOTAGE_CLUSTERS` | JSON list of clusters, in the order shown |
| `CABOTAGE_CLUSTER_NAME` | Optional. Names this cluster if its address isn't in the list |
| `CABOTAGE_CLUSTER_SWITCHER_ACCESS` | Who can switch: `admins` (default) or `users` |

Each cluster has:

| Key | Needed | What it does |
|-----|--------|--------------|
| `name` | yes | Up to 40 letters, digits, `.`, `_` or `-` |
| `url` | yes | Address with no path, like `https://cabotage.example.com` |
| `region` | no | Shown next to the name |
| `color` | no | `purple`, `blue`, `teal`, `rose`, `amber`, `emerald`, `white` or `dark` |

```hcl
cabotage_app_extra_config = {
  CABOTAGE_CLUSTERS = jsonencode([
    { name = "psfhosted", url = "https://cabotage.us-east-2.psfhosted.computer", color = "teal" },
    { name = "pypi", url = "https://cabotage.us-east-2.pypi.io", color = "amber" },
  ])
}
```

Cabotage won't start if the list has a mistake.

The color becomes the cluster's default theme color. People can still pick
their own in the theme menu.

## What people see

Everyone sees the cluster's name at the start of the top bar and the tab title,
and its color along the top edge and in the tab icon.

People allowed to switch can also open the switcher. It shows:

- each cluster, how fast it answers, and the page you'll land on
- by default, the closest page both clusters share: `/projects/acme/api` goes
  to `/projects`

<kbd>Tab</kbd> changes where you land. <kbd>⌘↵</kbd> opens a new tab.
<kbd>1</kbd>–<kbd>9</kbd> jump straight to a cluster. They can also add
clusters of their own, which only their browser remembers.
