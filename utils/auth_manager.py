#!/usr/bin/env python3
# Copyright (c) 2024 MOLO17
# Author: Daniele Angeli
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import os
import json
import logging
import secrets
import string
import traceback
from pathlib import Path

from utils.log import log_success, log_failure, lockfile_failure
from utils.gluesync_sdk_client import get_token, get_gluesync_client
from utils.core_hub_client import CoreHubClient

# Default paths and settings
DEFAULT_USER = os.getenv('DEFAULT_USER', 'admin')
DEFAULT_PASSWORD = os.getenv('DEFAULT_PASSWORD', '')
AUTH_TOKEN_PATH = os.path.join('/opt/config', 'auth_token.json')


class AuthManager:
    """
    Manages authentication with CoreHub using either SDK or direct credentials.
    Handles token retrieval, verification, and password changes.
    """
    
    def __init__(self, logger, core_hub_client, use_sdk=False):
        """
        Initialize the AuthManager
        
        Args:
            logger: The logger instance to use
            core_hub_client: The CoreHubClient instance
            use_sdk: Whether to use the SDK for authentication
        """
        self.logger = logger
        self.core_hub_client = core_hub_client
        self.use_sdk = use_sdk
        self.token = None
        self.username = DEFAULT_USER
        self.password = DEFAULT_PASSWORD

    def save_token(self, token):
        """Save the authentication token to a JSON file in the config directory."""
        try:
            # Create config directory if it doesn't exist
            os.makedirs(os.path.dirname(AUTH_TOKEN_PATH), exist_ok=True)

            # Create token JSON structure
            token_data = {
                "token": token
            }

            with open(AUTH_TOKEN_PATH, 'w') as f:
                json.dump(token_data, f, indent=2)
            self.logger.info(f"Authentication token saved successfully to {AUTH_TOKEN_PATH}")
            return True
        except Exception as e:
            self.logger.warning(f"Failed to save authentication token: {e}")
            return False

    def load_saved_token(self):
        """
        Load and verify a previously saved token
        
        Returns:
            str or None: The verified token or None if not available/valid
        """
        try:
            with open(AUTH_TOKEN_PATH, 'r') as f:
                token_data = json.load(f)
                token = token_data.get('token')
                if token:
                    # Verify login by attempting to authenticate
                    try:
                        check_token = self.core_hub_client.request(
                            '/pipelines',
                            method='GET',
                            token=token
                        )
                        if isinstance(check_token, list):
                            log_success(self.logger, "Successfully authenticated with saved token")
                            return token
                    except Exception as e:
                        if "401" in str(e):
                            self.logger.warning("Saved token is invalid, attempting to authenticate with credentials")
                        else:
                            raise e
        except FileNotFoundError:
            self.logger.info("No saved token found, attempting to authenticate with credentials")
        except Exception as e:
            self.logger.warning(f"Error loading saved token: {str(e)}")
        
        return None

    def get_sdk_token(self):
        """
        Attempt to get and verify a token from the SDK
        
        Returns:
            str or None: The verified SDK token or None if not available/valid
        """
        if not self.use_sdk:
            return None
            
        try:
            self.logger.info("Attempting to get token from Gluesync SDK")
            sdk_client = get_gluesync_client()
            if sdk_client:
                sdk_token = get_token()
                if sdk_token:
                    self.logger.info("Successfully retrieved token from Gluesync SDK")
                    # Verify the SDK token works
                    try:
                        check_token = self.core_hub_client.request(
                            '/pipelines',
                            method='GET',
                            token=sdk_token
                        )
                        if isinstance(check_token, list):
                            log_success(self.logger, "Successfully authenticated with SDK token")
                            return sdk_token
                        else:
                            self.logger.warning("SDK token verification returned unexpected response")
                    except Exception as e:
                        self.logger.warning(f"SDK token verification failed: {str(e)}")
        except Exception as e:
            self.logger.warning(f"Error retrieving SDK token: {str(e)}")
        
        return None

    def authenticate_with_credentials(self):
        """
        Authenticate using username and password credentials
        
        Returns:
            str or None: The authentication token or None if authentication failed
        """
        try:
            auth_response = self.core_hub_client.request(
                '/authentication/login',
                method='POST',
                body={'username': self.username, 'password': self.password}
            )
            token = auth_response.get('apiToken')
            if not token:
                log_failure(self.logger, "Failed to authenticate")
                return None

            # Handle password change if required
            change_required = auth_response.get('changeRequired', False)
            if change_required:
                self.logger.info("Password change required")
                token = self._handle_password_change(token)
            else:
                # Save the token if no password change was required
                self.save_token(token)
                
            return token
        except Exception as e:
            log_failure(self.logger, f"Authentication failed: {str(e)}")
            return None

    def _handle_password_change(self, token):
        """
        Handle the password change flow
        
        Args:
            token: The current authentication token
            
        Returns:
            str or None: The new token after password change or None if failed
        """
        # Generate a new random password
        new_password = self._generate_random_password()
        
        try:
            # Change password and get new token
            new_token = self._change_password(token, self.password, new_password)
            log_success(self.logger, f"Successfully changed password to: {new_password}")
            
            # Save the new password as the current password
            self.password = new_password
            
            # Save the new token
            self.save_token(new_token)
            
            return new_token
        except Exception as e:
            log_failure(self.logger, f"Password change failed, attempting to continue with current password: {str(e)}")
            # Try to get a fresh token with the current password
            try:
                auth_response = self.core_hub_client.request(
                    '/authentication/login',
                    method='POST',
                    body={'username': self.username, 'password': self.password}
                )
                token = auth_response.get('apiToken')
                if token:
                    # Save the token
                    self.save_token(token)
                    return token
                    
                log_failure(self.logger, "Failed to re-authenticate with current password")
            except Exception as re_auth_error:
                log_failure(self.logger, f"Re-authentication failed: {str(re_auth_error)}")
            
            return None

    def _change_password(self, token, old_password, new_password):
        """
        Change the user password and return the new token.
        
        Args:
            token: The current authentication token
            old_password: The current password
            new_password: The new password to set
            
        Returns:
            str: The new authentication token
        """
        response = self.core_hub_client.request(
            '/authentication/changePassword',
            method='POST',
            token=token,
            body={
                'oldPassword': old_password,
                'newPassword': new_password
            }
        )
        
        # Get the new token from the response
        new_token = response.get('apiToken')
        if not new_token:
            raise ValueError("No token returned after password change")
            
        return new_token

    def _generate_random_password(self) -> str:
        """
        Generate a random, secure password
        
        Returns:
            str: A random password with mixed case, numbers and symbols
        """
        symbols = [chr(i) for i in range(33, 47)]
        password = ""
        for _ in range(9):
            password += secrets.choice(string.ascii_lowercase)
        password += secrets.choice(string.ascii_uppercase)
        password += secrets.choice(string.digits)
        password += secrets.choice(symbols)
        return password

    def authenticate(self):
        """
        Main authentication method that tries all available methods
        
        Returns:
            str or None: The authentication token or None if all methods failed
        """
        # Try all authentication methods in order of preference
        
        # 1. First try to use a saved token
        self.token = self.load_saved_token()
        if self.token:
            return self.token
            
        # 2. Then try SDK authentication if enabled
        if self.use_sdk:
            self.token = self.get_sdk_token()
            if self.token:
                return self.token
                
        # 3. Finally, try direct credential authentication
        self.token = self.authenticate_with_credentials()
        if self.token:
            return self.token
            
        # If all authentication methods fail
        log_failure(self.logger, "All authentication methods failed")
        lockfile_failure()
        return None
