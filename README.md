# pretool

TUI tool for sending `SITE PRE` commands via the cbftp HTTPS/JSON API.

## Features

- Manage sites (cbftp name, group dirs per affil, active/inactive toggle)
- Manage sections with regexp patterns for automatic release detection
- List releases in group dirs across all active sites
- Check completeness via zipscript tag dirs (percent, nfo, sfv, Sample, Proof) before pre
- Preview and confirm commands before sending
- Send `SITE PRE` to multiple sites simultaneously with per-site section mapping
- Online check with latency measurement per site
- API request log with save-to-file option

## Requirements

- Python 3.11+
- A running [cbftp](https://cbftp.glftpd.io/) instance with the HTTPS API enabled

## Installation

```bash
git clone https://github.com/Nyxiuz/pretool.git
cd pretool
pip install -r requirements.txt
```

## Usage

```bash
python3 pretool.py
```

On first run a `config.toml` is created next to the script with default values. Open **Settings** from the main menu to configure the cbftp API connection (URL, password, SSL, timeout) and use the built-in connection test.

## Configuration

`config.toml` is generated automatically. Key sections:

| Section | Purpose |
|---|---|
| `[api]` | cbftp API URL, password, SSL verification, timeout |
| `[defaults]` | Pre command template, completeness tag patterns |
| `[[sites]]` | Site list with affils and section mappings |
| `[[sections]]` | Section names and release-matching regexps |

The pre command template supports `{release}` and `{section}` placeholders. Each site can define a `section_map` to translate global section names to site-local ones.

## Keybindings

### Main menu
| Key | Action |
|---|---|
| `Enter` | Open selected item |
| `q` | Quit |

### Sites / Sections
| Key | Action |
|---|---|
| `a` | Add |
| `e` | Edit |
| `d` | Delete |
| `Esc` | Back |

### Pre release
| Key | Action |
|---|---|
| `Enter` | Select release |
| `r` | Refresh listing |
| `Esc` | Back |

### Online check
| Key | Action |
|---|---|
| `r` | Refresh |
| `Esc` | Back |

### API log
| Key | Action |
|---|---|
| `s` | Save log to file |
| `Esc` | Back |

## License

Do whatever you want with it.
