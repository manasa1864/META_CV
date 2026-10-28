"""Answer pipeline: verified-path cache -> router -> factual (graph-verified) or reasoning tier.

Every stage is a generator that yields thinking-trace events for the UI and
returns its result, so the whole run can be streamed live.
"""
