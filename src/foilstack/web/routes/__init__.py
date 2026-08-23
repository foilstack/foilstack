"""Route modules.

app.py grew to fourteen hundred lines because every route landed in it by
default. Groups that stand on their own move here as routers; the ones that
share the page chrome are still in app.py, and moving those means making
`settings` something a route is handed rather than a module global bound at
import — a change worth doing deliberately rather than alongside this one.
"""
