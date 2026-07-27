"""Outside sources: language models, the open web corpus, and keyword data.

Each module here reaches something Doxa does not control, so each one is responsible for the
same discipline: report a failure as a failure. A source that could not be reached must never
reach a caller as an empty result, because "no citations found" and "Wikipedia was down" look
identical in a table and mean opposite things.
"""
