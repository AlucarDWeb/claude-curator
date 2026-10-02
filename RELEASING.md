# Releasing

Installs come from release tags, not from `main`. The plugin entry in `.claude-plugin/marketplace.json` names a tag and its commit sha, and Claude Code fetches exactly that commit. Commits on `main` reach users only when a release moves that pointer.

To ship version `X.Y.Z`:

1. Set `"version": "X.Y.Z"` in `.claude-plugin/plugin.json`, commit and push to `main`.
2. Tag that commit and push the tag:
   ```
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```
3. Publish the release: `gh release create vX.Y.Z --title vX.Y.Z --notes "..." --verify-tag`.
4. In `.claude-plugin/marketplace.json`, set the plugin entry's `version` to `X.Y.Z`, `ref` to `vX.Y.Z` and `sha` to the output of `git rev-parse vX.Y.Z^{commit}`. Run `claude plugin validate .`, then commit and push to `main`.

Claude Code decides whether an installed plugin is out of date by comparing version strings, so step 1 is required. Changing only the tag or the sha does not trigger an update.
