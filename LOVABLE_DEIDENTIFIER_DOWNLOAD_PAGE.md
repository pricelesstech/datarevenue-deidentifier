# Lovable prompt: De-Identifier download page (datarevenue.io/deidentifier)

Paste everything below the line into the **datarevenue.io** (marketing site) Lovable project.
The download links point at GitHub Releases of `pricelesstech/datarevenue-deidentifier`
(public; v1.1.0 published 2026-11-20). They always serve the newest release.

---

Add a new public page at **/deidentifier** (no login). Match the site's existing header,
footer, fonts and colors. Use navy #0B2A4A for headings and teal #0E8C8C for buttons and
accents. No em dashes anywhere in the copy. Do not add any form, upload field, tracking of
file names, or anything that sends data from the visitor: this page only offers downloads.

## 1. Hero

- Eyebrow: "Free tool for Data Revenue clients"
- Headline: "Share a safe sample of your data in minutes"
- Subhead: "Download the Data Revenue De-Identifier. It removes every patient and personal
  identifier on your own computer, then gives you one small file to send us. Nothing is
  uploaded, and it works without internet."
- Primary button: detect the visitor's operating system from `navigator.userAgent` /
  `navigator.userAgentData.platform`:
  - Windows: "Download for Windows" linking to
    `https://github.com/pricelesstech/datarevenue-deidentifier/releases/latest/download/DataRevenue-DeIdentifier-Setup.exe`
  - Mac: "Download for Mac" linking to
    `https://github.com/pricelesstech/datarevenue-deidentifier/releases/latest/download/DataRevenue-DeIdentifier-mac-AppleSilicon.dmg`
  - anything else (phone, Linux): show "Available for Windows and Mac. Open this page on
    your computer to download." and no button.
- Under the button, small links: "Windows (Windows 10 or 11)", "Mac with Apple chip
  (M1 or newer)", "Mac with Intel chip", each linking to its file:
  - `.../releases/latest/download/DataRevenue-DeIdentifier-Setup.exe`
  - `.../releases/latest/download/DataRevenue-DeIdentifier-mac-AppleSilicon.dmg`
  - `.../releases/latest/download/DataRevenue-DeIdentifier-mac-Intel.dmg`
  (full prefix `https://github.com/pricelesstech/datarevenue-deidentifier`)
- Small grey line: "About 100 MB. Free. No account needed."
- Right side (desktop) / below (mobile): a simple illustration card showing a shield with a
  check mark and three rows "Names removed", "IDs replaced with codes", "Text on X-rays
  blacked out", each with a teal check.

## 2. How it works (3 steps, numbered cards)

1. **Install** "Download and open the installer. It takes under a minute and needs no
   administrator rights."
2. **Add your data** "Drag in your spreadsheets (Excel or CSV) and images (DICOM X-rays, JPG,
   PNG, TIFF). Answer a few short questions about your data while it works."
3. **Send one file** "You get one .zip file with a de-identified sample and a summary of your
   whole data set. Send it to your Data Revenue contact."

## 3. Your data never leaves your computer (two-column section)

Left: heading "Built for privacy" and these bullets:
- "Runs entirely on your computer. No upload, no cloud, no login."
- "Works with Wi-Fi turned off, so you can prove nothing is sent."
- "Follows the HIPAA Safe Harbor method for de-identification."
- "The key that links study codes back to your patients stays with you."

Right: a compact table titled "What gets removed":
| Identifier | What happens |
|---|---|
| Names | Removed, and erased from notes |
| Addresses, city, county | Removed |
| ZIP codes | First 3 digits kept |
| Dates | Year only |
| Ages over 89 | Shown as 90+ |
| Phone, email, SSN, account numbers | Removed |
| Patient IDs | Replaced with random study codes |
| Text printed on X-rays | Found and blacked out |
| Hidden image data (DICOM, EXIF) | Stripped |

## 4. FAQ (accordion)

- **Windows says "Windows protected your PC". Is it safe?** "Yes. Windows shows this for
  new programs. Click More info, then Run anyway."
- **My Mac says the app can't be opened.** "Open System Settings, then Privacy & Security,
  scroll down and click Open Anyway next to Data Revenue De-Identifier."
- **How long does it take?** "A quick sample usually takes a few minutes. Processing every
  image in a large archive can take several hours, so most clients start with a sample."
- **What files does it accept?** "Excel (.xlsx), CSV, DICOM X-rays and scans, JPG, PNG,
  TIFF and BMP images. Other files are skipped and listed for you."
- **Does Data Revenue ever see my original files?** "No. Only the de-identified file you
  choose to send."
- **Which Mac do I have?** "Click the Apple menu, then About This Mac. If it says Chip:
  Apple M1 (or M2, M3, M4), choose Apple chip. If it says Processor: Intel, choose Intel."

## 5. Closing band

Navy background, white text: "Questions before you start? Talk to your Data Revenue
contact." with a teal button "Contact us" linking to the site's existing contact page.

## Other

- Add "De-Identifier" to the footer under Resources (or the closest existing group).
- Page title: "Data De-Identifier | Data Revenue". Meta description: "Free offline tool that
  removes patient and personal identifiers from spreadsheets and X-rays, then creates a
  safe data sample to share."
- Fully responsive; the OS-detected button must not flash the wrong label on load (render
  it after detection).

## Testing (read-only)

Open /deidentifier on desktop and on a phone width. Confirm the OS button label matches,
the three file links point to the exact URLs above, the FAQ opens and closes, and nothing
on the page submits or uploads anything. Do not change any other page.
