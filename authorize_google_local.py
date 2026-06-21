"""
Run this on a machine with a browser (e.g. your Mac), NOT the Pi.
The old urn:ietf:wg:oauth:2.0:oob flow is deprecated by Google and now
returns "Error 400: invalid_request". This uses run_local_server() instead,
which opens your default browser and listens on localhost for the redirect.

Requires the same credentials.json used on the Pi (Desktop app OAuth client).
If you don't have a copy on the Mac, scp it down:
  scp pi-ssh:~/Documents/webhook-server/credentials.json .

Install deps if needed:
  pip install google-auth-oauthlib google-api-python-client --break-system-packages
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
creds = flow.run_local_server(port=0)

with open("token.json", "w") as f:
    f.write(creds.to_json())

print("token.json saved successfully — now scp it to the Pi:")
print("  scp token.json pi-ssh:~/Documents/webhook-server/token.json")