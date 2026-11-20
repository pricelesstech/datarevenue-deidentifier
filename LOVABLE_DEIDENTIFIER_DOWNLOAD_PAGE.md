# Lovable prompt: De-Identifier page (datarevenue.io/deidentifier)

Paste everything below the line into the **datarevenue.io** (marketing site) Lovable project.
The tool itself is the browser version hosted at
`https://pricelesstech.github.io/datarevenue-deidentifier/` (GitHub Pages, from the public
repo). This page introduces it and sends clients there.

---

Add a new public page at **/deidentifier** (no login). Match the site's existing header,
footer, fonts and colors. Use navy #0B2A4A for headings and teal #0E8C8C for buttons and
accents. No em dashes anywhere in the copy. Do not add any form, upload field, or anything
that receives files on this page: the tool runs on its own page, inside the visitor's
browser.

## 1. Hero

- Eyebrow: "Free tool for Data Revenue clients"
- Headline: "Share a safe sample of your data in minutes"
- Subhead: "Drop in your spreadsheets and X-rays. Every patient and personal identifier is
  removed inside your own browser, and you get one small file to send us. Nothing is
  uploaded, and there is nothing to install."
- Primary button: "Open the De-Identifier" linking to
  `https://pricelesstech.github.io/datarevenue-deidentifier/` (same tab).
- Small grey line under it: "Free. No account. Works in Chrome, Edge, Safari and Firefox on
  a computer."
- Right side (desktop) / below (mobile): a simple illustration card showing a shield with a
  check mark and three rows "Names removed", "IDs replaced with codes", "Text on X-rays
  blacked out", each with a teal check.

## 2. How it works (3 numbered cards)

1. **Open the tool** "Click Open the De-Identifier. It loads in about 10 seconds."
2. **Add your data** "Drag in your spreadsheets (Excel or CSV) and images (DICOM X-rays, JPG,
   PNG, TIFF). Answer a few short questions about your data while it works."
3. **Send one file** "Download one .zip file with a de-identified sample and a summary of
   your whole data set, and send it to your Data Revenue contact."

## 3. Your data never leaves your computer (two-column section)

Left: heading "Built for privacy" and these bullets:
- "Your files are processed inside your browser tab. They are never uploaded to us or
  anyone else."
- "Once the tool has loaded you can turn off Wi-Fi and it keeps working, so you can see for
  yourself that nothing is sent."
- "Follows the HIPAA Safe Harbor method for de-identification."
- "You keep the key that links study codes back to your patients."
- "The code is public, so your IT team can check exactly what it does." (link "public" to
  `https://github.com/pricelesstech/datarevenue-deidentifier`)

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

- **Do you see my original files?** "No. Your files stay on your computer. Only the
  de-identified zip you choose to send ever reaches us."
- **How long does it take?** "Usually a few minutes for a sample of 500 records per
  spreadsheet and 25 images."
- **What files does it accept?** "Excel (.xlsx), CSV, DICOM X-rays and scans, JPG, PNG,
  TIFF and BMP images. Other files are skipped."
- **Can I use it on my phone?** "It is designed for a computer, where your data files are."
- **I have a very large archive.** "Start with a sample. If we need more, we will help you
  with the next step."

## 5. Closing band

Navy background, white text: "Questions before you start? Talk to your Data Revenue
contact." with a teal button "Contact us" linking to the site's existing contact page.

## Other

- Add "De-Identifier" to the footer under Resources (or the closest existing group).
- Page title: "Data De-Identifier | Data Revenue". Meta description: "Free tool that removes
  patient and personal identifiers from spreadsheets and X-rays inside your browser, then
  creates a safe data sample to share. Nothing is uploaded."
- Fully responsive.

## Testing (read-only)

Open /deidentifier on desktop and on a phone width. Confirm the button opens
`https://pricelesstech.github.io/datarevenue-deidentifier/`, the FAQ opens and closes, and
nothing on the page submits or uploads anything. Do not change any other page.
