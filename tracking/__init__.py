"""Video tracking pipeline — sibling to the Opta event warehouse.

Extract player/ball tracks from match video into parquet + Postgres aggregates.
Does not store full-match video in the warehouse.
"""

__version__ = '0.1.0'
