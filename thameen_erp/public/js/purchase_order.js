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
		// Offered either way — a PO already linked to a trip (the older
		// trip-raises-its-own-PO flow above) can still get its own,
		// separate pickup trip. `create_pickup_trip` warns rather than
		// silently overwriting that existing link.
		if (frm.doc.docstatus === 1) {
			frm.add_custom_button(__("Send Vehicle to Collect"), () => open_pickup_trip_dialog(frm))
				.addClass("btn-primary");
		}
	},
});

// An empty truck, sent to go collect this PO in person — nothing is loaded
// yet, so there is nothing to check here beyond which truck and who is
// driving it. The truck only actually gets loaded once this PO's own
// Purchase Receipt is submitted (see procurement.link_shortfall_receipt_to_trip).
function open_pickup_trip_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Send Vehicle to Collect {0}", [frm.doc.name]),
		fields: [
			{
				fieldname: "vehicle",
				fieldtype: "Link",
				options: "Vehicle",
				label: __("Vehicle"),
				reqd: 1,
				description: __("Only empty trucks are offered — this trip carries nothing until the Purchase Receipt is submitted."),
				get_query: () => ({
					query: "thameen_erp.overrides.vehicle_stock.empty_vehicle_query",
				}),
			},
			{
				fieldname: "driver",
				fieldtype: "Link",
				options: "Driver",
				label: __("Driver"),
				get_query: () => ({
					query: "thameen_erp.overrides.vehicle_load.driver_query",
				}),
			},
			{
				fieldname: "departure_time",
				fieldtype: "Datetime",
				label: __("Departure Time"),
				default: frappe.datetime.now_datetime(),
			},
		],
		primary_action_label: __("Create Trip"),
		primary_action(values) {
			frappe.call({
				method: "thameen_erp.overrides.procurement.create_pickup_trip",
				args: {
					purchase_order: frm.doc.name,
					vehicle: values.vehicle,
					driver: values.driver,
					departure_time: values.departure_time,
				},
				freeze: true,
				callback({ message }) {
					dialog.hide();
					if (message) frappe.set_route("Form", "Delivery Trip", message);
				},
			});
		},
	});
	dialog.show();
}
