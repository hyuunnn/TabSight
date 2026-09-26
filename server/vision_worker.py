"""Native vision libraries run out of process so their crashes cannot stop the app."""
import json
import sys
from pathlib import Path
from .vision import analyse_video

if __name__=='__main__':
    folder=Path(sys.argv[1])
    def progress(value):
        (folder/'vision-progress').write_text(str(value))
    result=analyse_video(folder/'source.mp4',progress,lambda:False)
    (folder/'vision-result.json').write_text(json.dumps(result,ensure_ascii=False))
