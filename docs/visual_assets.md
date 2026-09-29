# Third-party visual assets

ResearchPilot uses one icon family and hosts every asset locally. No remote fonts,
commercial templates, runtime CDN, or frontend framework was added.

| Asset | Pinned source | License | Usage |
| --- | --- | --- | --- |
| Lucide SVG icons, 0.468.0 | https://github.com/lucide-icons/lucide/tree/0.468.0/icons | ISC; upstream notice also identifies Feather-derived MIT portions | 23 symbols in `researchpilot/api/static/icons.svg`: navigation, composer, report actions, sources, empty states, document rows; compass also used in the favicon |

The unmodified upstream copyright and permission notice is distributed beside the
assets as `researchpilot/api/static/lucide-LICENSE.txt` and included in the wheel.
The SVG paths were extracted from the tagged upstream SVGs into a local sprite;
no third-party JavaScript is shipped.

Design reference: Linear's public redesign explanation
(https://linear.app/now/how-we-redesigned-the-linear-ui), particularly stable chrome,
consistent alignment and reduced visual noise. This was a principles reference;
no layout, proprietary artwork, component code or commercial template was copied.

Typography uses the operating system font stack. Skeletons and activity indicators
are small original CSS implementations and respect reduced-motion preferences.
