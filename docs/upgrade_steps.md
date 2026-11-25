# Upgrading Notion Automator

> **Draft.** The version-specific section lists changes that are already made on the development branch and not yet released.

## Every upgrade

1. **Stop the daemon.** Linux service: `sudo systemctl stop notion-daemon`. Windows service: stop "Notion Automator" in `services.msc`. Wait for it to stop before touching any file: two copies running at once will both write to Notion.
2. **Back up your local files.** Copy `config.toml`, `config_secrets.toml`, `config_hub_state.toml` and, once it exists, `config_automations.toml` somewhere safe. None of them is in the repository, so an update will not replace them, but a backup costs nothing.
3. **Get the new version.** `git pull` in the project folder, then `pip install -r requirements.txt` inside your virtual environment (the Notion client package is pinned there and the daemon refuses to start on an older one).
4. **Read the changes.** Check the release notes and the version-specific section below for anything you must change by hand.
5. **Make the changes.** Compare your `config.toml` with `config_example.toml`: new options appear there first, commented out, with an explanation.
6. **Start the daemon and read the log.** The first lines show the version, where the token was read from and whether the Configuration Hub is on. Fix anything the log reports before you rely on it.

## Next release (changes on the development branch)

- **The token file is renamed.** `secrets.toml` is now `config_secrets.toml`, so it sorts next to `config.toml`. Rename your file, and copy `config_secrets_example.toml` if you need the template. On Windows, run the `icacls` line from `install-service.ps1` again for the new name, because the access rights belong to the old file name. If you forget, the daemon stops with "No Notion token" and tells you the file was renamed.
- **The Configuration Hub is optional.** Without `config_hub_data_source_id` in `config.toml` the daemon runs without it. See "Setting up the Configuration Hub" in the README.
- **Switching a row off now takes effect without a restart.** Setting a config row's Status to anything other than Active stops that row's automations on the next poll. Removing a recurring-tasks block from an Active row stops it too.
