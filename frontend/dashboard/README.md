# Water Sentinel public dashboard (read-only)

One static page (`index.html`) that reads the ThingSpeak channel and shows live status, values and charts.
No server, no build step. It contains only the channel's **Read** API key (read-only). Never put the **Write** key here.

## Deploy on Netlify (2 minutes)
1. Open https://app.netlify.com/drop (sign in with Google/GitHub).
2. Drag this whole `frontend/dashboard` folder onto the page.
3. Netlify gives you a link like `https://random-name.netlify.app`. Open it on your phone. Done.
   (Site settings -> Change site name to get a nicer address.)

## Change channel or key
Edit the two constants near the top of the `<script>` in `index.html`: `CHANNEL` and `READ_KEY`, then drag the folder again.

## If the page says "load failed"
ThingSpeak channel -> Sharing: either tick "Share channel view with everyone", or keep it private (the Read key in the page still works).
Check the Channel ID and Read key on the channel's "API Keys" tab.

## Status codes the board sends (field 6)
0 starting, 1 normal, 2 anomaly, 3 sensor fault. Field 7 = driver sensor (1 temp, 2 TDS, 3 turbidity, 4 pH). Field 8 = driver z.
