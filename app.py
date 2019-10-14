import os, functools, collections
from typing import Dict, Tuple
from quart import Quart, jsonify, url_for, request, send_from_directory, redirect, session
import spotify

from pathlib import Path
root = Path(__file__).parent

app = Quart(__name__, static_folder='build')
app.secret_key = 'sup3rsp1cy'

from quart_cors import cors
app = cors(app, allow_origin="*")

# OAuth ###########################################################################################

scopes = """
playlist-read-collaborative
playlist-modify-private
playlist-modify-public
playlist-read-private

user-modify-playback-state
user-read-currently-playing
user-read-playback-state

user-read-private
user-read-email

user-library-modify
user-library-read

user-follow-modify
user-follow-read

user-read-recently-played
user-top-read

streaming
app-remote-control
""".split()
oauth2 = spotify.OAuth2(os.environ['SPOTIFY_CLIENT_ID'], os.environ['SPOTIFY_REDIRECT_URI'], scopes=scopes)


def require_user(func):
    @functools.wraps(func)
    def wrap(*args, **kwds):
        if not get_user():
            if request.is_json: # quart is missing is_xhr...
                return "login required", 401
            else:
                session['next'] = request.path
                return redirect(oauth2.url)
        return func(*args, **kwds)
    return wrap


@app.route('/login/authorized')
async def spotify_authorized():
    try:
        code = request.args['code']
    except KeyError:
        return f"Failed to authenticate with Spotify: {request.args['error']}"

    if session['next'] == '/python_console':
        return "user = await User.from_code(spotify.Client(os.environ['SPOTIFY_CLIENT_ID'], os.environ['SPOTIFY_CLIENT_SECRET']), '"+code+"', redirect_uri=os.environ['SPOTIFY_REDIRECT_URI'], refresh=True)"
    client = spotify.Client(os.environ['SPOTIFY_CLIENT_ID'], os.environ['SPOTIFY_CLIENT_SECRET']) # This errors if constructed outside of route
    user = await User.from_code(client, code, redirect_uri=os.environ['SPOTIFY_REDIRECT_URI'], refresh=True)
    users[id(user)] = user
    session['user_id'] = id(user)
    return redirect(session['next'])


# Main ############################################################################################

class User(spotify.models.User):

    # TODO self.library.get_all_tracks is being added shortly so this can go away
    # https://github.com/mental32/spotify.py/issues/22
    async def get_all_tracks(self):
        tracks = []
        for i in range(100):
            try:    tracks.extend(await self.library.get_tracks(limit=50, offset=50*i))
            except: break
        return tracks

    # Temporary fix to get token refreshing working
    # https://github.com/mental32/spotify.py/issues/20
    # Waiting for pull request to get pushed to pypy so I can get rid of this
    async def _refreshing_token(self, expires: int, token: str):
        while True:
            import asyncio
            await asyncio.sleep(expires-1)
            REFRESH_TOKEN_URL = "https://accounts.spotify.com/api/token?grant_type=refresh_token&refresh_token={refresh_token}"
            route = ("POST", REFRESH_TOKEN_URL.format(refresh_token=token))
            from base64 import b64encode
            auth = b64encode(":".join((os.environ['SPOTIFY_CLIENT_ID'], os.environ['SPOTIFY_CLIENT_SECRET'])).encode())
            try:
                data = await self.client.http.request(
                    route,
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "Authorization": f"Basic {auth.decode()}"}
                )

                expires = data["expires_in"]
                self.http.token = data["access_token"]
                print('token refreshed', data["access_token"])
            except:
                import traceback
                traceback.print_exc()

users = {}

def get_user() -> User:
    return users.get(session.get('user_id'))


class HostPlaylist(spotify.models.Playlist):

    @classmethod
    async def create(cls, owner, name, latLng=None):
        full_name = f"Intersection - {name}"
        playlists = await owner.get_playlists(limit=50) # get_all_playlists is being added soon...
        self = next((p for p in playlists if p.name == full_name), None)
        if self is None: # Empty playlist is considered False!!!
            self = await owner.create_playlist(full_name)

        self.__class__ = cls
        self.owner = owner
        self.name  = name
        self.latLng = latLng
        self.users = {}
        self.join_url = url_for('join', _external=True, owner_id=owner.id, name=name)
        return self

    async def add_tracks(self, user, tracks):
        self.users[user] = tracks
        counter = collections.Counter(track for user_tracks in self.users.values() for track in user_tracks)
        common = [(track, count) for track, count in counter.items() if count > 1]
        ordered = sorted(common, key=lambda x: x[1], reverse=True)
        most_common = [track for track, count in ordered]
        await self.replace_tracks(*most_common)
        return most_common

    def to_dict(self):
        return dict(
            owner    = self.owner.display_name,
            name     = self.name,
            latLng   = self.latLng,
            users    = [user.display_name for user in self.users],
            join_url = self.join_url
        )

host_playlists : Dict[Tuple[str, str], HostPlaylist] = {}


@app.route('/host/<name>')
@require_user
async def host(name):
    user = get_user()
    latLng = to_floats(request.args.get("latLng"))
    playlist = await HostPlaylist.create(user, name, latLng)
    host_playlists[(user.id, name)] = playlist
    return playlist.to_dict()


@app.route('/find')
async def find():
    playlists = host_playlists.values()
    latLng = to_floats(request.args.get('latLng'))
    if latLng:
        radius = request.args.get('radius', 100)
        from geopy.distance import distance
        def close(playlist):
            return playlist.latLng and distance(latLng, playlist.latLng).m < radius
        playlists = [p for p in playlists if close(p)]

    return jsonify([p.to_dict() for p in playlists])


@app.route('/join/<owner_id>/<name>')
@require_user
async def join(owner_id, name):
    user = get_user()
    host_playlist = host_playlists[(owner_id, name)]

    if request.args.get('give') == 'playlists':
        playlists = await user.get_playlists(limit=50) # get_all_playlists is being added soon...
        # TODO Need to compare ids since owner.__class__ != self.__class__
        owned = [p for p in playlists if p.owner.id == user.id]
        tracks = [track for playlist in owned for track in await playlist.get_all_tracks()]
    else:
        tracks = await user.get_all_tracks()

    most_common = await host_playlist.add_tracks(user, tracks)
    # user.follow_playlist(host_playlist) uncomment when added to pypy
    return host_playlist.to_dict()


@app.route("/leave/<owner_id>/<name>")
@require_user
def leave(owner_id, name):
    user = get_user()
    host_playlist = host_playlists[(owner_id, name)]
    del host_playlist.users[user]


@app.route('/reset')
def reset():
    global users, host_playlists
    users = {}
    host_playlists = {}
    return "great success"


to_floats = lambda val: val and [float(v) for v in val.split(",")]

# Serve React App #################################################################################

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
@require_user
def serve(path):
    if path and (Path(app.static_folder) / path).exists():
        return send_from_directory(app.static_folder, path)
    else:
        return send_from_directory(app.static_folder, 'index.html')


# Run #############################################################################################

# set QUART_APP=app:app && quart run --host=0.0.0.0 --port=80
if __name__ == '__main__':
    app.run(host="0.0.0.0", debug=True)
