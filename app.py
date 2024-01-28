from datetime import timedelta
import os
import json
import hashlib
import random
import re
import requests
from functools import wraps
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    abort,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)
from flask_caching import Cache
from flask_socketio import SocketIO, emit
from wslpath import wslpath
from catbox import Uploader
from dotenv import load_dotenv

load_dotenv()

SEARCH_FOLDER = Path(os.environ["SEARCH_FOLDER"])

RUSTYPASTE_URL = os.environ["RUSTYPASTE_URL"]
RUSTYPASTE_AUTH = os.environ["RUSTYPASTE_AUTH"]
RUSTYPASTE_SESSION = requests.Session()
RUSTYPASTE_SESSION.headers.update({"Authorization": RUSTYPASTE_AUTH})

CATBOX_AUTH = os.environ["CATBOX_AUTH"]

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["SECRET_KEY"] = "secret!"
app.config["CACHE_TYPE"] = "simple"  # You can choose Redis, Memcached, etc.
cache = Cache(app)

socketio = SocketIO(app)

FILE_TYPES = ["jpeg", "jpg", "png", "gif", "mp4"]

# md5: url
uploaded_json = Path("uploaded.json")
if not uploaded_json.exists():
    uploaded_json.write_text("{}")
uploaded: dict[str, str] = json.loads(uploaded_json.read_text())
runtime_md5: dict[Path, str] = {}

uploader = Uploader(token=CATBOX_AUTH)


def get_all_files():
    return [f for f in SEARCH_FOLDER.rglob("*") if f.suffix[1:] in FILE_TYPES]


all_files = get_all_files()


@app.route("/")
def index():
    query = request.args.get("query", "")  # Get the query parameter
    return render_template("index.html", query=query)


cache_timeout = timedelta(days=365).total_seconds()


@app.route("/media/<path:filename>")
def media(filename):
    if not (SEARCH_FOLDER / filename).exists():
        abort(404)
    response = make_response(send_from_directory(SEARCH_FOLDER, filename))
    response.cache_control.max_age = cache_timeout
    del response.cache_control.no_cache
    return response


def cache_socketio(event: str, key_prefix="socketio_cache_"):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            cache_key = key_prefix + f.__name__ + str(args) + str(kwargs)
            rv = cache.get(cache_key)
            if rv is not None:
                emit(event, rv)
                return rv
            rv = f(*args, **kwargs)  # Execute the function.no_cache
            cache.set(cache_key, rv)  # Cache without expiry
            return rv

        return decorated_function

    return decorator


def argument_cache(key_prefix="argument_cache_"):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            cache_key = key_prefix + f.__name__ + str(args) + str(kwargs)
            rv = cache.get(cache_key)
            if rv is not None:
                return rv
            rv = f(*args, **kwargs)  # Execute the function.no_cache
            cache.set(cache_key, rv)  # Cache without expiry
            return rv

        return decorated_function

    return decorator


def clear_app_cache():
    global all_files
    all_files = get_all_files()

    for key in list(cache.cache._cache):
        if (
            key.startswith("socketio_cache_")
            or key.startswith("api_cache_")
            or key.startswith("argument_cache_")
        ):
            cache.delete(key)


@argument_cache()
def file_info(file: Path) -> dict[str, Any]:
    info = {
        "url": url_for("media", filename=str(file.relative_to(SEARCH_FOLDER))),
        "path": str(file.relative_to(SEARCH_FOLDER)),
        "name": file.name,
        "md5": hashlib.md5(file.read_bytes()).hexdigest(),
        "type": "video" if file.suffix == ".mp4" else "image",
        "tags": list(basename_to_tags_fast_and_unsafe(file.stem)),
    }

    if app.debug:
        info["full_path"] = str(wslpath(file, from_wsl_path_to_windows_path=True))
    return info


def available_tags(file_list: list[str]) -> list[tuple[str, int]]:
    alias_mapping = {
        alias: next(iter(sorted(group, reverse=True)))
        for group in TAG_ALIASES
        for alias in group
    }

    # Modify the existing loop to replace tags with their alias
    modified_available_tags = {}
    for f in file_list:
        used_tags = set()
        for tag in basename_to_tags_fast_and_unsafe(f.split("/")[-1]):
            primary_tag = alias_mapping.get(tag, tag)
            if primary_tag in used_tags:
                continue
            if primary_tag not in modified_available_tags:
                modified_available_tags[primary_tag] = 0
            modified_available_tags[primary_tag] += 1
            used_tags.add(primary_tag)

    # Proceed with the existing sorting
    available_tags = sorted(
        modified_available_tags.items(), key=lambda x: x[1], reverse=True
    )
    return available_tags


@app.route("/api/search", methods=["GET"])
@cache.cached(query_string=True, key_prefix="api_cache_")
def api_search(data):
    query = request.args.get("query", "")
    tags = request.args.get("tags", [])

    try:
        results = handle_search(query=query, tags=tags)
    except Exception:
        return {"error": "An error occurred while searching"}
        raise
    return results


@socketio.on("search")
@cache_socketio("results")
def socket_search(data):
    print("Searching for", data)
    query = data.get("query", "")
    tags = data.get("tags", [])

    try:
        results = handle_search(query=query, tags=tags)
    except Exception:
        emit("error", "An error occurred while searching")
        raise
    emit("results", results)


@argument_cache()
def handle_search(query: str = "", tags: list[str] = []) -> dict[str, Any]:
    print(f"Searching for {query} with tags {tags}")
    query = query.lower()

    # Handling search for files without tags with "[]"
    if query == "[]":
        results = [f for f in all_files if "(" not in f.name and ")" not in f.name]
    else:
        query_words = query.split()
        tag_queries = [f"({tag})" for tag in tags]
        results = [
            f
            for f in all_files
            if (
                all(tag in f.name for tag in tag_queries)
                and all(qw in f.name.lower() for qw in query_words)
            )
        ]

    files = [file_info(file) for file in results]
    result_tags = available_tags([f["path"] for f in files])

    result = {"query": query, "files": files, "tags": result_tags}

    return result


@app.route("/api/tags", methods=["GET"])
@cache.cached(query_string=True, key_prefix="api_cache_")
def api_tags():
    min = request.args.get("min", 1)
    sort_by_count = request.args.get("sort_by_count", False)
    try:
        tags = handle_tags(min=min, sort_by_count=sort_by_count)
    except Exception:
        return {"error": "An error occurred while fetching the tags"}
        raise
    return tags


@socketio.on("tags")
@cache_socketio("tags")
def socket_tags(data: dict):
    min = data.get("min", 1)
    sort_by_count = data.get("sort_by_count", False)
    try:
        tags = handle_tags(min=min, sort_by_count=sort_by_count)
    except Exception:
        emit("error", "An error occurred while fetching the tags")
        raise
    emit("tags", tags)


def handle_tags(min: int = 1, sort_by_count: bool = False) -> list[tuple[str, int]]:
    tags = {}
    for f in all_files:
        basename = f.stem
        if "(" not in basename and ")" not in basename:
            continue
        for tag in basename_to_tags(basename):
            if tag not in tags:
                tags[tag] = 0
            tags[tag] += 1

    tags = {tag: count for tag, count in tags.items() if count >= min}
    if sort_by_count:
        sorted_tags = sorted(tags.items(), key=lambda x: x[1], reverse=True)
    else:
        sorted_tags = sorted(tags.items(), key=lambda x: x[0])
    return sorted_tags


TAG_ALIASES = [
    {"😭", "sob", "cry", "sad"},
    {"😄", "smile", "happy"},
    {"😂", "laugh", "lol"},
    {"usa", "america", "murica"},
    {"💵", "money", "cash", "yen", "$"},
    {"kill myself", "kms"},
    {"🖕", "kill yourself", "kys", "f you"},
    {"🔫", "shoot", "gun"},
    {"❓", "what", "?", "confused"},
    {"😮", "surprised", "shock", "!", "😯"},
    {"🤮", "ew", "yuck", "🤢", "disgusted"},
    {"👌", "approve", "thumbs up", "+1", "👍", "ok"},
    {"🍆", "boner", "horny", "hot", "lewd"},
    {"🏳️‍🌈", "gay", "faggot"},
    {"😏", "smirk", "smug", "heh"},
    {"💊", "based", "redpilled"},
    {"🤔", "think"},
]


def tags_to_basename(tags: set[str]) -> str:
    """Converts a list of tags to a string that can be used as a file name."""
    tag_set = set([tag.lower() for tag in tags])

    # Fill in all aliases
    for tags in TAG_ALIASES:
        if tag_set.intersection(tags):
            tag_set = tag_set.union(tags)

    return "".join([f"({tag})" for tag in sorted(tag_set)])


def basename_to_tags(basename: str) -> set[str]:
    """Converts a file name to a list of tags with adjusted rules."""
    tags = set()

    # Adjustments: if no parentheses are found, the whole basename is a tag
    if "(" not in basename and ")" not in basename:
        return set(basename.split())

    depth = 0
    tag = ""
    for char in basename:
        if char == "(":
            if depth == 0 and tag:  # Capture any text before an opening parenthesis
                tags.add(tag.strip())
                tag = ""
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and tag:
                tags.add(tag.strip())
                tag = ""
        elif depth > 0 or (depth == 0 and char != "("):
            tag += char

    # Include the last tag if the string doesn't end with a closing parenthesis
    if tag:
        tags.add(tag.strip())

    to_remove = {"", " "}
    tags = tags.difference(to_remove)

    return tags


def basename_to_tags_fast_and_unsafe(basename: str) -> set[str]:
    # do it with regex lol
    return set(re.findall(r"\((.*?)\)", basename))


@app.route("/api/rename", methods=["POST"])
def api_rename():
    data = request.get_json()
    old_name = SEARCH_FOLDER / data["old"]
    new_name_input = data["new"]
    search_query = data.get("searchQuery", "")

    try:
        result = handle_rename(
            old_name=old_name, new_name_input=new_name_input, search_query=search_query
        )
    except Exception:
        return {"error": "An error occurred while renaming the file"}
        raise

    if result == -1:
        return {"error": "New name is the same as the old name"}
    elif result:
        return {"success": "Successfully renamed file", "results": result}
    else:
        return {"success": "Successfully renamed file"}


@socketio.on("rename")
def socket_rename(data):
    old_name = SEARCH_FOLDER / data["old"]
    new_name_input = data["new"]
    search_query = data.get("searchQuery", "")
    try:
        result = handle_rename(
            old_name=old_name, new_name_input=new_name_input, search_query=search_query
        )
    except Exception:
        emit("error", "An error occurred while renaming the file")
        raise

    if result == -1:
        emit("error", "New name is the same as the old name")
    elif result:
        emit("success", "Successfully renamed file")
        emit("results", result)
    else:
        emit("success", "Successfully renamed file")
        emit("rename_success")


def handle_rename(
    old_name: Path, new_name_input: str, search_query: dict[str, Any] = {}
):
    print(f"Renaming {old_name.relative_to(SEARCH_FOLDER)} to {new_name_input}")
    base_name, extension = Path(new_name_input).stem, Path(new_name_input).suffix

    counter = 0
    if len(parts := base_name.split(".")) > 1 and parts[-1].isdigit():
        counter = int(parts[-1])
        base_name = ".".join(parts[:-1])

    tags = basename_to_tags(base_name)
    if not counter:
        new_name_input = tags_to_basename(tags) + extension
    else:
        new_name_input = tags_to_basename(tags) + f".{counter}{extension}"

    if Path(new_name_input).suffix.lstrip(".") not in FILE_TYPES:
        new_name_input += old_name.suffix

    if Path(new_name_input).suffix == ".jpeg":
        new_name_input = new_name_input.replace(".jpeg", ".jpg")

    if old_name.name == new_name_input:
        print("TEST")
        return -1

    new_name = old_name.with_name(new_name_input)

    if old_name.exists():
        # Handle potential duplicate file names
        base_name, extension = new_name.stem, new_name.suffix
        counter = 1

        while new_name.exists():
            new_name = SEARCH_FOLDER / f"{base_name}.{counter}{extension}"
            counter += 1

        print(f"Renaming {old_name} to {new_name}")
        old_name.rename(new_name)

        # Update all_files list
        clear_app_cache()
        if search_query:
            result = handle_search(
                query=search_query["query"], tags=search_query["tags"]
            )
            filename = str(new_name.relative_to(SEARCH_FOLDER))
            if filename in result["files"]:
                result["files"].remove(filename)
            result["files"].insert(0, filename)
            return result


@app.route("/api/delete", methods=["POST"])
def api_delete():
    data = request.get_json()
    filename = SEARCH_FOLDER / data["file"]
    search_query = data.get("searchQuery", {})
    try:
        result = handle_delete(filename, search_query)
    except Exception:
        return {"error": "An error occurred while deleting the file"}
        raise
    if result:
        return {"success": "Successfully deleted file", "results": result}
    else:
        return {"success": "Successfully deleted file"}


@socketio.on("delete")
def socket_delete(data):
    filename = SEARCH_FOLDER / data["file"]
    search_query = data.get("searchQuery", {})
    try:
        result = handle_delete(filename, search_query)
    except Exception:
        emit("error", "An error occurred while deleting the file")
        raise
    if result:
        emit("results", result)
    else:
        emit("delete_success")


def handle_delete(filename: Path, search_query: dict[str, Any] = {}):
    if filename.exists():
        print(f"Deleting {filename}")
        filename.unlink()

        clear_app_cache()
        if search_query:
            return handle_search(query=search_query["query"], tags=search_query["tags"])


@app.route("/api/open", methods=["POST"])
def api_open():
    data = request.get_json()
    filename = SEARCH_FOLDER / data["file"]
    try:
        handle_open(filename)
        return {"success": "Successfully opened file"}
    except FileNotFoundError:
        return {"error": "an error occurred while opening the file"}
    except Exception:
        return {"error": "an error occurred while opening the file"}
        raise


@socketio.on("open")
def socket_open(data):
    try:
        filename = SEARCH_FOLDER / data["file"]
        handle_open(filename)
        emit("open_success")
    except FileNotFoundError:
        emit("error", "an error occurred while opening the file")
    except Exception:
        emit("error", "an error occurred while opening the file")
        raise


def handle_open(filename: Path):
    if filename.exists():
        windows_path = wslpath(str(filename), from_wsl_path_to_windows_path=True)
        cmd = f'explorer.exe /select, "{windows_path}"'
        os.system(cmd)
    else:
        raise FileNotFoundError


@app.route("/random/redirect/<tag>", methods=["GET"])
def random_file_url_by_tag(tag):
    if not tag:
        abort(400)

    tagged_files = [f for f in all_files if f"({tag})" in f.stem]
    if not tagged_files:
        abort(404)

    random_file = random.choice(tagged_files)
    redirect_url = url_for(
        "static", filename=str(random_file.relative_to(SEARCH_FOLDER))
    )
    return redirect(redirect_url)


@app.route("/random/inline/<tag>", methods=["GET"])
def random_file_by_tag(tag):
    file_type = request.args.get("type", "image")
    suffixes: list[str] = []
    if file_type == "image":
        suffixes = [".jpeg", ".jpg", ".png"]
    elif file_type == "video":
        suffixes = [".mp4"]
    elif file_type == "gif":
        suffixes = [".gif"]

    if not tag:
        abort(400)

    tagged_files = [
        f
        for f in all_files
        if f"({tag})" in f.stem and (not suffixes or f.suffix in suffixes)
    ]
    if not tagged_files:
        abort(404)

    random_file = random.choice(tagged_files)
    return send_file(random_file)


@app.route("/api/catbox", methods=["POST"])
def api_catbox():
    data = request.get_json()
    filename = SEARCH_FOLDER / data["file"]
    try:
        url = upload_to_bin(filename)
        return {"success": "Successfully uploaded file", "url": url}
    except Exception:
        return {"error": "An error occurred while uploading the file"}
        raise


@socketio.on("catbox")
def socket_catbox(data):
    filename = SEARCH_FOLDER / data["file"]
    try:
        url = upload_to_bin(filename)
        emit("catbox_success", url)
    except Exception:
        emit("error", "An error occurred while uploading the file")
        raise


def add_to_uploaded(file: Path, url: str):
    md5 = hashlib.md5(file.read_bytes()).hexdigest()
    uploaded[md5] = url
    runtime_md5[file] = md5
    Path("uploaded.json").write_text(json.dumps(uploaded))


def upload_to_bin(file: Path) -> str:
    if file in runtime_md5:
        return uploaded[runtime_md5[file]]

    md5 = hashlib.md5(file.read_bytes()).hexdigest()
    if md5 in uploaded:
        runtime_md5[file] = md5
        return uploaded[md5]

    # url = upload_to_catbox(file)
    url = upload_to_rustybin(file)
    add_to_uploaded(file, url)
    return url


def upload_to_rustybin(file: Path) -> str:
    print(f"Uploading {file} rustypaste")
    response = RUSTYPASTE_SESSION.post(
        RUSTYPASTE_URL, files={"file": file.read_bytes()}
    )
    if response.status_code != 200:
        raise Exception("Failed to upload to rustypaste")
    url = response.text.strip()
    print(f"Uploaded {file} to {url}")
    return url


def upload_to_catbox(file: Path):
    print(f"Uploading {file} to catbox")
    data = uploader.upload(file.suffix.lstrip("."), file_raw=file.read_bytes())
    print(f"Uploaded {file} to {data['file']}")
    return data["file"]


if __name__ == "__main__":
    socketio.run(app)
