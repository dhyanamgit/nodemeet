from nodemeet.sfu import MediaBackend


class FakeSFU(MediaBackend):
    """Pretends to be an SFU so topology switching can be tested without aiortc."""

    available = True

    def __init__(self):
        self.pubs = set()

    async def publish(self, room_id, peer_id, offer):
        self.pubs.add((room_id, peer_id))
        return {"sdp": "answer-" + offer["sdp"], "type": "answer"}

    async def subscribe(self, room_id, sub, pub, offer, kinds=None):
        if (room_id, pub) not in self.pubs:
            raise ValueError("not publishing")
        return {"sdp": "sub-answer", "type": "answer"}

    def publishers(self, room_id):
        return [p for r, p in self.pubs if r == room_id]

    async def unpublish(self, room_id, peer_id):
        self.pubs.discard((room_id, peer_id))

    async def remove_peer(self, room_id, peer_id):
        self.pubs.discard((room_id, peer_id))

    async def remove_room(self, room_id):
        self.pubs = {k for k in self.pubs if k[0] != room_id}
