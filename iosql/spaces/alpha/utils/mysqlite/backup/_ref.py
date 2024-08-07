


class Cursor(object):

    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self, *args, **kwargs):
        self.connect()

    def __exit__(self, *args, **kwargs):
        self.close()
