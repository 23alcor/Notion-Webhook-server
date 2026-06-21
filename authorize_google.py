from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"

auth_url, _ = flow.authorization_url(prompt="consent")
print(f"\nOpen this URL in your browser:\n{auth_url}\n")

code = input("Paste the authorization code here: ")
flow.fetch_token(code=code)

with open("token.json", "w") as f:
    f.write(flow.credentials.to_json())

print("token.json saved successfully")
