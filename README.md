# things-bridge

> **Status: under construction.** Research is done; the implementation is in progress.

A command-line tool and MCP server that lets you (and Claude) read and manage your
[Things 3](https://culturedcode.com/things/) to-dos on macOS — using **only the methods
Cultured Code documents as safe**.

## Safety first

Cultured Code's article [Third-Party AI Tools and Things](https://culturedcode.com/things/support/articles/5510170/)
lists the safe ways to connect tools to Things (URL scheme, Apple Shortcuts, AppleScript, Mail to Things)
and warns that anything else — especially touching the Things database — can corrupt data.

things-bridge follows that strictly:

| | How | Never |
|---|---|---|
| **Changes** | [Things URL scheme](https://culturedcode.com/things/support/articles/2803573/) only | Writing to the Things database |
| **Reading** | [AppleScript](https://culturedcode.com/things/support/articles/4562654/), read-only queries only | Opening the Things database or files, even read-only |
| **Removing** | Complete / cancel (reversible) | Deleting, trashing, or emptying the Trash |
| **Credentials** | Your URL-scheme auth token, stored in the macOS Keychain | Asking for Things Cloud credentials |

## License

MIT — see [LICENSE](LICENSE).

*things-bridge is an independent project and is not affiliated with or endorsed by Cultured Code.
Things is a trademark of Cultured Code GmbH & Co. KG.*
