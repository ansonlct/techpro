# Third-party notices

The Google News URL decoding approach in `src/news_core.py` interoperates with the undocumented `Fbv4je` batchexecute protocol and is informed by the MIT-licensed `decodeGoogleNewsUrl.ts` implementation by Ruslan Gainutdinov (huksley), copyright 2022–2024.

The implementation in this project is a Python standard-library adaptation with additional redirect fallback, SQLite migration, validation and failure isolation.
