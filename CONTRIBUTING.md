# Contributing

## Setup

```sh
git clone https://github.com/luv-agarwal/ck-browser && cd ck-browser
python3 -m unittest discover tests
```

ck-browser needs Python 3.11+ and uses the standard library only. Do not add a dependency.

## Before you start

Open an issue first for anything beyond a small fix, so we can agree on the approach.

## Rules for a change

- Test what you change, with `unittest`. Tests must not touch the network, and they must pass
  without Chrome and without a Jev key.
- A change to a verb or a flag updates `--help`, the README, `skills/ck-browser/SKILL.md` and
  `AGENTS.md` in the same change.
- Make no speed or benchmark claims beyond the README's "Why use it" paragraph. A speed claim cites measured per-run numbers and names what it is compared with.
- Keep user-facing wording plain: short sentences, facts, no marketing.

## Commit messages

One line, in the imperative, with a scope prefix, for example:

```
fix(runner): fail a drag whose target scrolled away
```

## Licence

By contributing you agree that your contribution is licensed under the MIT licence of this
project (see `LICENSE`).
