"""Ad Studio blueprints, one per area of the app."""
from creative_intelligence.webapp.routes import (adpack, aiads, compose, core, drive, gallery, ideas, images, ops, pages,
                                                 products, results, videos)

BLUEPRINTS = [m.bp for m in (pages, products, ideas, images, compose, aiads, gallery, videos, drive, core, results, ops, adpack)]
