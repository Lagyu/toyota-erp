// Website pages (sign-in, partner portals): a text alternative for the logo Frappe's sign-in
// page shows without one (WCAG 1.1.1), named as the desk names the same image.
document.querySelectorAll("img.app-logo:not([alt])").forEach((img) => img.setAttribute("alt", __("App Logo")));
