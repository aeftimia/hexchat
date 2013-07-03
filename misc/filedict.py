import sys

if sys.version_info < (3, 0):
    from filedict2x import *
else:
    from filedict3x import *
