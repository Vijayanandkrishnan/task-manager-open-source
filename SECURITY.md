# Security

## Supported Use

This is a self-hosted application. You are responsible for server hardening, HTTPS, backups, access control, and Telegram bot permissions in your deployment.

## Never Commit

Do not commit:

- `tasks.db` or any exported database.
- `.secret_key`, `.env`, API tokens, Telegram bot tokens, deploy keys, or webhook secrets.
- `app.log`, backups, screenshots containing customer data, or handoff notes.

These files are ignored by `.gitignore`, but always check before pushing.

## Production Checklist

- Set a strong `SECRET_KEY`.
- Use HTTPS and secure cookies.
- Rotate first-run credentials immediately.
- Restrict server access and file permissions.
- Review role permissions from the Permissions page.
- Use a dedicated Telegram group for sales/HR notifications.
- Disable or protect deployment endpoints if you do not use them.
- Back up SQLite data outside the repository.

## Reporting Issues

If you find a vulnerability, open a private advisory or contact the maintainer directly before publishing details.
