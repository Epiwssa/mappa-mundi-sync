# Privacy policy: Mappa Mundi and Mappa Mundi Sync

*Effective 9 November 2026*

Mappa Mundi is a field mapping app for Android and iOS. Mappa Mundi Sync is its QGIS plugin. Both are made by Epiwssa. This policy explains what data they handle.

## The short version

- No account is needed, and there are no ads, analytics or tracking.
- Epiwssa does not run any server, and does not receive, collect or store your data.
- Your data stays on your phone and goes only to the QGIS computer you pair with.

## Data the app handles

All of this is processed on your phone. It leaves the phone only as described in the next section.

- **Location:** while the app is open, it shows your position on the map and records the GPS points and tracks you choose to collect. It does not use location in the background.
- **Camera and photos:** used to scan pairing QR codes and to take or attach photos of features.
- **Map data and files:** the layers, features, attributes and files you open, create or edit.
- **Phone name and app ID:** a name for your phone (by default its model name) and a random ID the app creates when first opened. They are sent with each sync so QGIS can tell paired phones apart. The ID is not your phone's hardware ID and is deleted when you uninstall the app.

## Where data goes

- **Your QGIS computer:** when you pair the app with QGIS, it exchanges project data, edits and photos with that computer over your local network. Everything sent between the phone and that computer is encrypted (AES-GCM) with a key from the pairing QR code, so others on the same network can't read or change it. That computer is yours, or belongs to whoever runs the QGIS project, and is not operated by Epiwssa.
- **Map tile servers:** to show online basemaps, the app downloads map tiles from the tile servers set in your project (by default OpenStreetMap). Like any web request, these servers can see your IP address and which map area is being viewed. Their own privacy policies apply, for example https://osmfoundation.org/wiki/Privacy_Policy.
- **Sharing you start:** if you choose to send a project or file, for example with WhatsApp, Bluetooth or email, it goes wherever you send it.

Nothing else is sent anywhere. No data is sold or shared with third parties.

## Storage and deletion

Mappa Mundi stores data only in the app's private storage on your phone. Epiwssa holds no copy, so there is nothing to request from us: you delete it yourself, straight away.

**To delete one project from Mappa Mundi:**

1. Open Mappa Mundi. The list "Projects on this phone" appears.
2. Tap **⋯** next to the project.
3. Tap **Delete**, then **Delete** again to confirm.

This removes the project's features, attributes, photos, GPS tracks, offline map tiles and any edits not yet synced. It is deleted immediately and nothing is kept.

**To delete everything:** uninstall Mappa Mundi, or go to Android **Settings → Apps → Mappa Mundi → Storage → Clear storage**. This deletes all projects and settings at once.

**Data already synced to QGIS** is in your own (or your organisation's) QGIS project, not with Epiwssa. Delete it there in QGIS. **Map tile servers** such as OpenStreetMap follow their own retention policies.

Questions: mrepiwssa@gmail.com

## Children

The app is a professional tool. It is not aimed at children and does not knowingly collect data from them.

## Changes

If this policy changes, the new version will be published at this address with a new effective date.

## Contact

Epiwssa, mrepiwssa@gmail.com
