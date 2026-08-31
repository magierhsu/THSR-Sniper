import React, { useEffect } from 'react';
import { isTokenExpired } from '@/utils/tokenUtils';

interface VersionCheckerProps {
  onUpdateDetected?: () => void;
}

const VersionChecker: React.FC<VersionCheckerProps> = ({ onUpdateDetected }) => {
  useEffect(() => {
    const checkVersion = () => {
      try {
        // Simple version check based on build time from meta tag
        const metaTag = document.querySelector('meta[name="build-time"]');
        const currentBuildTime = metaTag?.getAttribute('content') || Date.now().toString();
        const storedBuildTime = localStorage.getItem('build_time');
        
        if (!storedBuildTime) {
          // First visit
          localStorage.setItem('build_time', currentBuildTime);
          console.log('First visit detected, version stored');
          return;
        }
        
        if (storedBuildTime !== currentBuildTime) {
          // Build has changed
          console.log('New build detected, clearing cache and reloading...', {
            old: storedBuildTime,
            new: currentBuildTime
          });
          
          onUpdateDetected?.();
          
          // Clear all possible caches
          if ('caches' in window) {
            caches.keys().then(names => {
              names.forEach(name => caches.delete(name));
            });
          }
          
          // Preserve the active session while clearing stale build data.
          const essentialData = {
            build_time: currentBuildTime,
            authStorage: localStorage.getItem('auth-storage'),
            authToken: localStorage.getItem('auth_token'),
            refreshToken: localStorage.getItem('refresh_token'),
            notificationHistory: localStorage.getItem('notificationHistory'),
          };
          
          localStorage.clear();
          
          localStorage.setItem('build_time', essentialData.build_time);
          if (essentialData.authStorage) {
            localStorage.setItem('auth-storage', essentialData.authStorage);
          }
          if (essentialData.authToken) {
            localStorage.setItem('auth_token', essentialData.authToken);
          }
          if (essentialData.refreshToken) {
            localStorage.setItem('refresh_token', essentialData.refreshToken);
          }
          if (essentialData.notificationHistory) {
            localStorage.setItem('notificationHistory', essentialData.notificationHistory);
          }
          
          // Force reload
          window.location.reload();
          return;
        }
        
        console.log('No version update needed');
      } catch (error) {
        console.error('Version check failed:', error);
      }
    };

    const checkTokenValidity = () => {
      // Check if we have tokens but they might be expired
      const token = localStorage.getItem('auth_token');
      
      if (token) {
        console.log('Checking token validity on page load...');
        if (isTokenExpired(token)) {
          console.log('Token is expired, triggering logout...');
          // Trigger logout event
          window.dispatchEvent(new CustomEvent('auth-logout'));
        } else {
          console.log('Token is still valid');
        }
      }
    };

    // Run both checks
    checkVersion();
    checkTokenValidity();
  }, [onUpdateDetected]);

  // Don't render anything, this is a background component
  return null;
};

export default VersionChecker;
