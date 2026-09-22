"""Explicit operator CLI for assigning verified Identity Platform custom claims."""

import argparse


def main():
    parser = argparse.ArgumentParser(
        description="Assign a Doci role to an existing Identity Platform user"
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--uid", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument(
        "--role", required=True, choices=["submitter", "analyst", "reviewer", "approver", "admin"]
    )
    args = parser.parse_args()
    import firebase_admin
    from firebase_admin import auth

    firebase_admin.initialize_app(options={"projectId": args.project})
    user = auth.get_user(args.uid)
    claims = dict(user.custom_claims or {})
    claims.update(tenant_id=args.tenant, role=args.role)
    auth.set_custom_user_claims(args.uid, claims)
    print("Claims updated. Ask the user to sign out and sign back in to refresh the ID token.")


if __name__ == "__main__":
    main()
