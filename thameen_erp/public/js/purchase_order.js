// Purchase Order — just a view link to whatever Delivery Trip is already
// attached to it (a Direct-from-Supplier trip set up by hand on the trip's
// own Trip Route field points its Purchase Order button back here).
//
// Planning a trip off cement bought here happens later, once the goods are
// actually received — see purchase_receipt.js. Receiving first keeps stock
// and accounts accurate the moment the supplier delivers, instead of only
// when a truck eventually gets around to loading it.

frappe.ui.form.on("Purchase Order", {
	refresh(frm) {
		if (frm.doc.custom_delivery_trip) {
			frm.add_custom_button(frm.doc.custom_delivery_trip,
				() => frappe.set_route("Form", "Delivery Trip", frm.doc.custom_delivery_trip), __("View"));
		}
	},
});
