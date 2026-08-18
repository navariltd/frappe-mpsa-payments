# Copyright (c) 2024, Navari Limited and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from .b2c_payment_disbursement_reference import (
    is_valid_receiver_contact,
    sanitise_phone_number,
)


class TestB2CPaymentDisbursementReference(FrappeTestCase):
    def test_validate_fails_if_amount_is_less_than_10(self):
        """Test that the validate method raises a ValidationError if the amount is less than 10"""
        doc = frappe.get_doc(
            {
                "doctype": "MPesa B2C Employee Payment Item",
                "amount": 5,
                "record_amount": 100,
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.validate()

    def test_validate_fails_if_amount_is_greater_than_record_amount(self):
        """Test that the validate method raises a ValidationError if the amount is greater than record_amount"""
        doc = frappe.get_doc(
            {
                "doctype": "MPesa B2C Employee Payment Item",
                "amount": 200,
                "record_amount": 100,
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.validate()

    def test_validate_fails_if_partyb_is_invalid(self):
        """Test that the validate method raises a ValidationError if partyb is invalid"""
        doc = frappe.get_doc(
            {
                "doctype": "MPesa B2C Employee Payment Item",
                "partyb": "some_invalid_number",
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.validate()

    def test_validate_passes_if_partyb_is_valid(self):
        """Test that the validate method does not raise a ValidationError if partyb is valid"""
        doc = frappe.get_doc(
            {
                "doctype": "MPesa B2C Employee Payment Item",
                "partyb": "+254712345678",
            }
        )
        try:
            doc.validate()
        except frappe.ValidationError:
            self.fail("validate() raised ValidationError unexpectedly!")

    def test_sanitise_phone_number(self):
        """Test that the sanitise_phone_number function correctly sanitises a phone number"""
        test_cases = [
            ("0712345678", "254712345678"),
            ("+254712345678", "254712345678"),
            ("254712345678", "254712345678"),
            ("0712345678 ", "254712345678"),
            (" 0712345678", "254712345678"),
            ("0712345678 +", "254712345678"),
            ("0712345678  ", "254712345678"),
        ]

        for input_number, expected_output in test_cases:
            self.assertEqual(sanitise_phone_number(input_number), expected_output)

    def test_sanitise_phone_number_invalid(self):
        """Test that the sanitise_phone_number function does not modify an invalid phone number"""
        test_cases = [
            ("+25471234567", "+25471234567"),
            ("07123456789", "07123456789"),
            ("25471234567", "25471234567"),
            ("0712345678a", "0712345678a"),
            ("0712345678 ", "0712345678 "),
            ("0112345678", "0112345678"),
        ]

        for input_number, expected_output in test_cases:
            self.assertEqual(sanitise_phone_number(input_number), expected_output)

    def test_is_valid_receiver_contact(self):
        """Test that the is_valid_receiver_contact function correctly identifies valid and invalid contacts"""
        valid_contacts = [
            "254712345678",
            "+254712345678",
            "0712345678",
        ]

        invalid_contacts = [
            "07123456789",
            "25471234567",
            "+25471234567",
            "0712345678a",
        ]

        for contact in valid_contacts:
            self.assertTrue(is_valid_receiver_contact(contact))

        for contact in invalid_contacts:
            self.assertFalse(is_valid_receiver_contact(contact))

    def test_is_valid_reciever_contact_for_011_phone_numbers(self):
        """Test that the is_valid_receiver_contact function correctly identifies 011 phone numbers as valid"""
        invalid_contacts = [
            "0112345678",
            "01123456789",
            "+254112345678",
            "254112345678",
        ]

        for contact in invalid_contacts:
            self.assertTrue(is_valid_receiver_contact(contact))
