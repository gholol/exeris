

from exeris.app import app

if False:
    app.run("0.0.0.0", debug=False)
else:
    app.run("127.0.0.1", debug=True)
