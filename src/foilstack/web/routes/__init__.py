"""Route modules.

app.py grew to fourteen hundred lines because every route landed in it by
default. Groups that stand on their own live here as routers, split by the
thing they act on rather than by whether they render HTML or return JSON: the
route that confirms a scan and the screen offering the button are one decision,
and separating them by response type would put its two halves in two files.

What made the split possible was moving settings out of a module global and
into `deps.settings_dep`. A global bound at import belongs to whichever module
imported first, so every route reading it had to live beside that binding.

**Declaration order survives into the route table.** Routers are matched in the
order they are included, and routes within a router in the order they are
declared, so a same-shape pair like `/api/inventory/delete` and
`/api/inventory/{item_id}` still depends on which comes first. See the note at
the top of inventory.py.
"""
