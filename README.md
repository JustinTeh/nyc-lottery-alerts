# NYC housing lottery alerts

Once a day this makes **one request** to Housing Connect: the same search the website runs when you enter a household size and income. The site returns the open rental lotteries your household can apply to. The script then filters by borough and bedroom size, adds income-band details from the city's Open Data feed when available, and sends you a digest with a link to each listing. It never logs in and never applies for you. You apply yourself from the link.

## Set it up (about 10 minutes)

1. Fork or copy this repo.
2. Copy `config.example.json` to `config.local.json` and fill in your numbers. `config.local.json` is git-ignored, so your income and assets never get committed.
3. Pick a private ntfy topic name, for example `nyc-housing` plus a few random characters (anyone who knows it can read it, so make it hard to guess). Install the ntfy app on your phone and subscribe to that topic.
4. In the repo go to Settings > Secrets and variables > Actions and add these **repository secrets**:
   - `CONFIG_JSON`: the full contents of your `config.local.json`
   - `NTFY_TOPIC`: your topic name
5. Optional email: also add `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_TO`. For Gmail use `smtp.gmail.com`, `587`, and an app password.
6. Open the Actions tab and run "Daily housing lottery alerts" once by hand to confirm it works. After that it runs daily on its own.

Live runs only log match counts, so your income never appears in the (public) Actions logs. The full digest goes to your phone and email.

## Your filters (config.local.json)

- `household_size`, `annual_income`: sent to Housing Connect's own search, which returns lotteries whose income range includes you. `assets` is printed in each digest as a reminder (the list data has no asset limits).
- `exclude_boroughs`: `["SI"]` skips Staten Island.
- `bedrooms`: studio and 1br. A lottery must have at least one unit of these sizes.
- `likely_bands`, `maybe_bands`: how the city feed's income bands are tagged. 
- `reminder_days`: re-alerts you when a lottery you already saw is this many days from closing.

## Run it locally

```
python3 lottery_alerts.py --config config.example.json --hc-fixture sample_hc.json --fixture sample_data.json --dry-run   # offline test, no personal data needed
python3 lottery_alerts.py --dry-run                                                          # live, sends nothing
```

## Things to know

- **Always check the listing.** Housing Connect's list gives one income range across all household sizes and no asset limits. Open each listing's Available Units table to confirm your income fits your household size and your assets are under the asset limit.
- The Housing Connect API is undocumented and could change or block automated requests. If it fails, the script falls back to the city feed (NYC Open Data dataset `vy5i-a666`) and says so in the digest. That feed trails the live site and leaves out some lottery types, so it can miss listings.
- The script ignores any lottery whose end date has passed (the feed sometimes keeps old lotteries marked "Active").
- Sales lotteries are not included, only rentals.
