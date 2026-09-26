import os
import tempfile
import atexit

_data=tempfile.TemporaryDirectory(prefix='tabsight-tests-')
os.environ['TABSIGHT_DATA_DIR']=_data.name
atexit.register(_data.cleanup)
