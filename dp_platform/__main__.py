"""python -m dp_platform"""

import uvicorn

from . import config

if __name__ == "__main__":
    uvicorn.run("dp_platform.main:app", host=config.HOST, port=config.PORT)
