from webapp import app

if __name__ == "__main__":
    # 0.0.0.0 so the container's port mapping / reverse proxy can reach it;
    # 127.0.0.1 (Flask's default) is only reachable from inside the container.
    app.run(host="0.0.0.0", port=5000, debug=True)
