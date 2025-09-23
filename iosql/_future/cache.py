

class Cache(object):

    def __init__(self, engine):
        self._engine = engine

    def get(self, key):
        pass

    def set(self, key, value): 
        if self.cache_full(): 
            self.try_release_cache() 

    def cache_full(self):
        pass

    def try_release_cache(self):
        pass
