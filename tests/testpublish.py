from os import environ
import os
from twisted.internet.defer import inlineCallbacks

from autobahn.twisted.util import sleep
from autobahn.twisted.wamp import ApplicationSession, ApplicationRunner


class ClientSession(ApplicationSession):
    """
    An application component that publishes an event every second.
    """

    @inlineCallbacks
    def onJoin(self, details):
        print("session attached")
        counter = 0
        while True:
            print('backend publishing com.myapp.hello', counter)
            self.publish('com.myapp.hello', "Hello World %d"%counter)
            counter += 1
            yield sleep(1)


if __name__ == '__main__':
    url = os.environ.get('CBURL', 'wss://azurekong.hertzai.com:8445/wss')
    realm = os.environ.get('CBREALM', 'realm1')

    # any extra info we want to forward to our ClientSession (in self.config.extra)
    extra = {
        'foobar': 'A custom value'
    }

    runner = ApplicationRunner(url=url, realm=realm, extra=extra)
    runner.run(ClientSession, auto_reconnect=True)
