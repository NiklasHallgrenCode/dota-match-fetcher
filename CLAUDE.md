# Instructions for Claude

## Git workflow

- Don't open pull requests. When a change is done and checked, merge it straight into `main` and push.
- Before pushing, fetch `origin/main` and rebase onto it: the fetch workflow commits new matches to `main` every day.
- Push to `main` with `git push origin HEAD:main`, and also push the session's working branch if one was assigned.
